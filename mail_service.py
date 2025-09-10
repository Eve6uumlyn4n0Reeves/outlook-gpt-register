from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import email as py_email
import httpx
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from fastapi import HTTPException
import os
import tempfile


# Constants consistent with main.py
ACCOUNTS_FILE = "accounts.json"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SERVER = "outlook.live.com"
IMAP_PORT = 993

log = logging.getLogger(__name__)



def _atomic_dump_json(path: str | os.PathLike[str], data: dict) -> None:
    """Write JSON to a temp file and atomically replace target.
    Ensures same-directory temp file to keep os.replace atomic on Windows.
    """
    dir_path = os.path.dirname(os.fspath(path)) or "."
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="._tmp_", suffix=".json", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _decode_header_value(header_value: str) -> str:
    if not header_value:
        return ""
    try:
        decoded_parts = decode_header(str(header_value))
        out = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    out += part.decode(charset or "utf-8", "replace")
                except Exception:
                    out += part.decode("utf-8", "replace")
            else:
                out += str(part)
        return out
    except Exception:
        return str(header_value) if header_value else ""


def _extract_email_content(msg: py_email.message.EmailMessage) -> Tuple[str, str]:
    body_plain = ""
    body_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", ""))
            if "attachment" in cdisp.lower():
                continue
            try:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    txt = payload.decode(charset, errors="replace")
                    if ctype == "text/plain" and not body_plain:
                        body_plain = txt
                    elif ctype == "text/html" and not body_html:
                        body_html = txt
            except Exception as e:
                log.warning("decode part failed: %s", e)
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                content = payload.decode(charset, errors="replace")
                ctype = msg.get_content_type()
                if ctype == "text/plain":
                    body_plain = content
                elif ctype == "text/html":
                    body_html = content
                else:
                    body_plain = content
        except Exception as e:
            log.warning("decode body failed: %s", e)
    return body_plain, body_html


# =====================
# Accounts IO helpers
# =====================
async def get_account_credentials(email_id: str) -> Any:
    """Return a simple object with attribute access (email, refresh_token, client_id)."""
    from types import SimpleNamespace
    try:
        if not Path(ACCOUNTS_FILE).exists():
            raise HTTPException(status_code=404, detail="Account not found")
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        if email_id not in accounts:
            raise HTTPException(status_code=404, detail="Account not found")
        acc = accounts[email_id]
        return SimpleNamespace(email=email_id, refresh_token=acc["refresh_token"], client_id=acc["client_id"])
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read accounts file")
    except Exception as e:
        log.error("get_account_credentials: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_all_accounts() -> Dict[str, Dict[str, str]]:
    try:
        if not Path(ACCOUNTS_FILE).exists():
            return {}
        return await asyncio.to_thread(_read_accounts_sync)
    except Exception as e:
        log.error("get_all_accounts: %s", e)
        return {}


def _read_accounts_sync() -> Dict[str, Dict[str, str]]:
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:

        log.error("Failed to decode accounts file")
        return {}



async def save_multiple_accounts_batch(credentials_list: List[Any]) -> None:
    try:
        await asyncio.to_thread(_save_multiple_accounts_sync, credentials_list)
        log.info("Batch saved %d accounts", len(credentials_list))
    except Exception as e:
        log.error("save_multiple_accounts_batch: %s", e)
        raise HTTPException(status_code=500, detail="Failed to batch save accounts")


def _save_multiple_accounts_sync(credentials_list: List[Any]):
    accounts = {}
    if Path(ACCOUNTS_FILE).exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    for c in credentials_list:
        rec = {"refresh_token": c.refresh_token, "client_id": c.client_id}
        # 持久化可选 password
        pwd = getattr(c, "password", None)
        if pwd:
            rec["password"] = pwd



        accounts[c.email] = rec
    _atomic_dump_json(ACCOUNTS_FILE, accounts)


async def save_account_credentials(email_id: str, credentials: Any) -> None:
    try:
        await asyncio.to_thread(_save_account_sync, email_id, credentials)
        log.info("Account saved: %s", email_id)
    except Exception as e:
        log.error("save_account_credentials: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save account")


def _save_account_sync(email_id: str, credentials: Any):
    accounts = {}
    if Path(ACCOUNTS_FILE).exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    rec = {"refresh_token": credentials.refresh_token, "client_id": credentials.client_id}
    pwd = getattr(credentials, "password", None)
    if pwd:
        rec["password"] = pwd
    accounts[email_id] = rec
    _atomic_dump_json(ACCOUNTS_FILE, accounts)


async def delete_accounts(emails: List[str]) -> Dict[str, int]:
    try:
        if not Path(ACCOUNTS_FILE).exists():
            return {"deleted": 0, "not_found": len(emails)}
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        deleted = 0
        not_found = 0
        for em in emails:
            if em in accounts:
                del accounts[em]; deleted += 1
            else:
                not_found += 1
        _atomic_dump_json(ACCOUNTS_FILE, accounts)
        return {"deleted": deleted, "not_found": not_found}
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read accounts file")
    except Exception as e:
        log.error("delete_accounts: %s", e)
        raise HTTPException(status_code=500, detail="Failed to delete accounts")


# =====================
# OAuth and IMAP
# =====================
# Simple in-memory token cache: key=(client_id, refresh_token) -> (token, expiry_ts)
_token_cache: Dict[str, Tuple[str, float]] = {}

def _token_cache_key(credentials: Any) -> str:
    return f"{credentials.client_id}:{credentials.refresh_token}"

async def get_access_token(credentials: Any, check_only: bool = False) -> Optional[str]:
    # Check cache first
    try:
        k = _token_cache_key(credentials)
        token, exp = _token_cache.get(k, (None, 0.0))  # type: ignore
        if token and time.time() < exp:
            return token
    except Exception:
        pass

    data = {
        "client_id": credentials.client_id,
        "grant_type": "refresh_token",
        "refresh_token": credentials.refresh_token,
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(TOKEN_URL, data=data)
            resp.raise_for_status()
            token_data = resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return None if check_only else (_raise_401())

            # Cache with conservative expiry
            try:
                ttl = int(token_data.get("expires_in", 3600))
            except Exception:
                ttl = 3600
            now = time.time()
            # expire 60s earlier; keep within [60s, 3600s]
            ttl_eff = max(60, min(ttl - 60, 3600)) if ttl >= 120 else max(60, ttl)
            _token_cache[k] = (access_token, now + ttl_eff)

            return access_token
    except httpx.HTTPError:
        return None if check_only else (_raise_401())
    except Exception:
        return None if check_only else (_raise_500("Token acquisition failed"))


def _raise_401():
    raise HTTPException(status_code=401, detail="Invalid credentials")


def _raise_500(msg: str):
    raise HTTPException(status_code=500, detail=msg)


class EmailCache:
    def __init__(self, ttl: int = 300):
        self.cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self.ttl = ttl

    def _key(self, email: str, folder: str, page: int, page_size: int) -> str:
        return f"{email}:{folder}:{page}:{page_size}"

    def get(self, email: str, folder: str, page: int, page_size: int) -> Optional[Dict[str, Any]]:
        k = self._key(email, folder, page, page_size)
        if k in self.cache:
            data, ts = self.cache[k]
            if time.time() - ts < self.ttl:
                return data
            del self.cache[k]
        return None

    def set(self, email: str, folder: str, page: int, page_size: int, data: Dict[str, Any]):
        self.cache[self._key(email, folder, page, page_size)] = (data, time.time())

    def clear_user(self, email: str):
        keys = [k for k in self.cache.keys() if k.startswith(f"{email}:")]
        for k in keys:
            del self.cache[k]


_email_cache = EmailCache()


async def list_emails(credentials: Any, folder: str, page: int, page_size: int, force_refresh: bool = False) -> Dict[str, Any]:
    if force_refresh:
        _email_cache.clear_user(credentials.email)
    cached = _email_cache.get(credentials.email, folder, page, page_size)
    if cached:
        return cached

    access_token = await get_access_token(credentials)

    def _sync() -> Dict[str, Any]:
        imap_client = None
        try:
            imap_client = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            auth = f"user={credentials.email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
            imap_client.authenticate("XOAUTH2", lambda x: auth)

            folders = ["INBOX", "Junk"] if folder == "all" else (["INBOX"] if folder == "inbox" else ["Junk"])
            all_meta: List[Dict[str, Any]] = []
            for fname in folders:
                try:
                    imap_client.select(f'"{fname}"', readonly=True)
                    status, messages = imap_client.search(None, "ALL")
                    if status != "OK" or not messages or not messages[0]:
                        continue
                    ids = messages[0].split()[::-1]  # newest first
                    for mid in ids:
                        all_meta.append({"message_id_raw": mid, "folder": fname})
                except Exception as e:
                    log.warning("folder %s error: %s", fname, e)
                    continue

            total = len(all_meta)
            start = (page - 1) * page_size
            end = start + page_size
            window = all_meta[start:end]

            # group by folder
            window.sort(key=lambda x: x["folder"])  # stable for groupby
            email_items: List[Dict[str, Any]] = []
            for fname, group in _groupby(window, key=lambda x: x["folder"]):
                imap_client.select(f'"{fname}"', readonly=True)
                ids_to_fetch = [it["message_id_raw"] for it in group]
                if not ids_to_fetch:
                    continue
                seq = b",".join(ids_to_fetch)
                status, data = imap_client.fetch(seq, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)] FLAGS)")
                if status != "OK" or not data:
                    continue
                # Robust parsing: some IMAP servers return a list with tuples and None separators
                for item in data:
                    try:
                        if not isinstance(item, tuple) or len(item) < 2:
                            continue
                        meta, header_data = item[0], item[1]
                        if not isinstance(meta, (bytes, bytearray)) or not isinstance(header_data, (bytes, bytearray)):
                            continue
                        match = re.match(rb"(\d+)\s+\(", meta)
                        if not match:
                            continue
                        fetched_id = match.group(1)
                        msg = py_email.message_from_bytes(header_data)
                        subject = _decode_header_value(msg.get("Subject")) or "(No Subject)"
                        from_email = _decode_header_value(msg.get("From")) or "(Unknown Sender)"
                        date_str = msg.get("Date", "")
                        try:
                            formatted_date = parsedate_to_datetime(date_str).isoformat() if date_str else datetime.now().isoformat()
                        except Exception:
                            formatted_date = datetime.now().isoformat()
                        email_items.append({
                            "message_id": f"{fname}-{fetched_id.decode()}",
                            "folder": fname,
                            "subject": subject,
                            "from_email": from_email,
                            "date": formatted_date,
                            "is_read": False,
                            "has_attachments": False,
                            "sender_initial": (re.search(r"([a-zA-Z])", from_email).group(1).upper() if re.search(r"([a-zA-Z])", from_email) else "?"),
                        })

                    except Exception as e:
                        log.warning("parse fetch item failed: %s", e)
                        continue

            email_items.sort(key=lambda x: x["date"], reverse=True)
            result = {
                "email_id": credentials.email,
                "folder_view": folder,
                "page": page,
                "page_size": page_size,
                "total_emails": total,
                "emails": email_items,
            }
            _email_cache.set(credentials.email, folder, page, page_size, result)
            return result
        except Exception as e:
            log.error("list_emails error: %s", e)
            raise HTTPException(status_code=500, detail="Failed to retrieve emails")
        finally:
            try:
                if imap_client is not None:
                    imap_client.logout()
            except Exception:
                pass

    return await asyncio.to_thread(_sync)


def _groupby(iterable: List[Dict[str, Any]], key):
    from itertools import groupby as _gb
    return [(k, list(g)) for k, g in _gb(iterable, key=key)]


async def get_email_details(credentials: Any, message_id: str) -> Dict[str, Any]:
    try:
        folder_name, msg_id = message_id.split("-", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message_id format")

    access_token = await get_access_token(credentials)

    def _sync() -> Dict[str, Any]:
        imap_client = None
        try:
            imap_client = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            auth = f"user={credentials.email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
            imap_client.authenticate("XOAUTH2", lambda x: auth)
            imap_client.select(f"\"{folder_name}\"", readonly=True)
            status, data = imap_client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data:
                raise HTTPException(status_code=404, detail="Email not found")
            raw_email = data[0][1]
            msg = py_email.message_from_bytes(raw_email)
            subject = _decode_header_value(msg.get("Subject", "(No Subject)"))
            from_email = _decode_header_value(msg.get("From", "(Unknown Sender)"))
            to_email = _decode_header_value(msg.get("To", "(Unknown Recipient)"))
            date_str = msg.get("Date", "")
            try:
                formatted_date = parsedate_to_datetime(date_str).isoformat() if date_str else datetime.now().isoformat()
            except Exception:
                formatted_date = datetime.now().isoformat()
            body_plain, body_html = _extract_email_content(msg)
            return {
                "message_id": message_id,
                "subject": subject,
                "from_email": from_email,
                "to_email": to_email,
                "date": formatted_date,
                "body_plain": body_plain or None,
                "body_html": body_html or None,
            }
        except HTTPException:
            raise
        except Exception as e:
            log.error("get_email_details error: %s", e)
            raise HTTPException(status_code=500, detail="Failed to retrieve email details")
        finally:
            try:
                if imap_client is not None:
                    imap_client.logout()
            except Exception:
                pass

    return await asyncio.to_thread(_sync)
