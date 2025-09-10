from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from types import SimpleNamespace
import mail_service as ms


@dataclass
class OtpConfig:
    subject_contains: str = "OpenAI"
    code_regex: str = r"\b(\d{6})\b"
    poll_interval_sec: int = 3
    poll_timeout_sec: int = 180
    junk_first: bool = True
    from_contains: Optional[str] = None
    max_age_min: int = 15


async def _cred(email: str) -> SimpleNamespace:
    d = await ms.get_account_credentials(email)
    return SimpleNamespace(email=d.email, refresh_token=d.refresh_token, client_id=d.client_id)


def _recent_enough(iso_datetime: str, *, max_age_min: int) -> bool:
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat((iso_datetime or "").replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_min * 60
    except Exception:
        return False


def _is_candidate(item: Dict[str, Any], cfg: OtpConfig) -> bool:
    subj = (item.get("subject") or "").lower()
    if cfg.subject_contains.lower() not in subj:
        return False
    if cfg.from_contains:
        if cfg.from_contains.lower() not in (item.get("from_email") or "").lower():
            return False
    if not _recent_enough(item.get("date") or "", max_age_min=cfg.max_age_min):
        return False
    return True


async def poll_for_code(email: str, cfg: OtpConfig) -> str:
    deadline = time.time() + cfg.poll_timeout_sec
    code_re = re.compile(cfg.code_regex)
    seen: set[str] = set()
    start_ts = time.time()

    def _iso_to_ts(iso_str: str) -> float:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat((iso_str or "").replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0.0

    while time.time() < deadline:
        cred = await _cred(email)
        resp = await ms.list_emails(cred, folder="all", page=1, page_size=50, force_refresh=True)
        msgs = list(resp.get("emails") or [])
        if cfg.junk_first:
            msgs = sorted(msgs, key=lambda x: 0 if (x.get("folder") or "").lower() == "junk" else 1)
        for it in msgs:
            mid = it.get("message_id")
            if mid in seen:
                continue
            # only messages that arrived after polling started
            if _iso_to_ts(it.get("date")) + 1 < start_ts:
                continue
            if not _is_candidate(it, cfg):
                continue
            detail = await ms.get_email_details(cred, mid)
            seen.add(mid)
            text = (detail.get("body_plain") or "") + "\n" + (detail.get("body_html") or "")
            m = code_re.search(text)
            if m:
                return m.group(1) if m.groups() else m.group(0)
        await asyncio.sleep(max(1, cfg.poll_interval_sec))

    # Final fallback: re-scan within window ignoring start_ts guard
    try:
        cred = await _cred(email)
        resp = await ms.list_emails(cred, folder="all", page=1, page_size=50, force_refresh=True)
        msgs = list(resp.get("emails") or [])
        if cfg.junk_first:
            msgs = sorted(msgs, key=lambda x: 0 if (x.get("folder") or "").lower() == "junk" else 1)
        for it in msgs:
            if not _is_candidate(it, cfg):
                continue
            detail = await ms.get_email_details(cred, it.get("message_id"))
            text = (detail.get("body_plain") or "") + "\n" + (detail.get("body_html") or "")
            m = code_re.search(text)
            if m:
                return m.group(1) if m.groups() else m.group(0)
    except Exception:
        pass

    raise TimeoutError("OTP polling timeout")
