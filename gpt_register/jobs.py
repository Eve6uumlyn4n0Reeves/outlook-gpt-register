from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List
import sqlite3

from .core import utc_now_iso


# In-memory run state + on-disk index/DB
RUNS: Dict[str, Dict[str, Any]] = {}
_TASKS: Dict[str, asyncio.Task] = {}

RUN_LOGS_DIR = Path("run-logs")
RUN_LOGS_DIR.mkdir(exist_ok=True)
INDEX_FILE = RUN_LOGS_DIR / "index.json"
DB_PATH = RUN_LOGS_DIR / "runs.sqlite"


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        c.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, status TEXT, created_at TEXT, updated_at TEXT)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                email TEXT,
                status TEXT,
                attempts INTEGER,
                duration_sec REAL,
                error_json TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _db_execute(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _persist_index() -> None:
    data = {}
    for rid, rec in RUNS.items():
        data[rid] = {
            "status": rec.get("status"),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "results_count": len(rec.get("results", [])),
        }
    INDEX_FILE.write_text(json.dumps({"runs": data}, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(run_id: str, results: List[Dict[str, Any]]) -> None:
    p = RUN_LOGS_DIR / f"run_{run_id}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


_init_db()


def create_run(req: Dict[str, Any]) -> str:
    run_id = uuid.uuid4().hex[:12]
    now = utc_now_iso()
    RUNS[run_id] = {
        "status": "queued",
        "request": req,
        "results": [],
        "created_at": now,
        "updated_at": now,
        "last_event": None,
        "control": {"cancel": False, "pause": False},
    }
    _persist_index()
    try:
        _db_execute(
            "INSERT OR REPLACE INTO runs(run_id, status, created_at, updated_at) VALUES(?,?,?,?)",
            (run_id, "queued", now, now),
        )
    except Exception:
        pass
    return run_id


async def _should_cancel(run_id: str) -> bool:
    return bool(RUNS.get(run_id, {}).get("control", {}).get("cancel"))


async def _wait_if_paused(run_id: str):
    while bool(RUNS.get(run_id, {}).get("control", {}).get("pause")):
        await asyncio.sleep(0.5)


def cancel_run(run_id: str) -> None:
    if run_id in RUNS:
        RUNS[run_id].setdefault("control", {})["cancel"] = True
        if RUNS[run_id].get("status") == "queued":
            RUNS[run_id]["status"] = "cancelled"
        RUNS[run_id]["updated_at"] = utc_now_iso()
        # Try cancelling task if executing
        task = _TASKS.get(run_id)
        if task and not task.done():
            task.cancel()
        _persist_index()
        try:
            _db_execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (RUNS[run_id]["status"], RUNS[run_id]["updated_at"], run_id),
            )
        except Exception:
            pass


def pause_run(run_id: str) -> None:
    if run_id in RUNS:
        RUNS[run_id].setdefault("control", {})["pause"] = True
        RUNS[run_id]["updated_at"] = utc_now_iso()
        _persist_index()


def resume_run(run_id: str) -> None:
    if run_id in RUNS:
        RUNS[run_id].setdefault("control", {})["pause"] = False
        RUNS[run_id]["updated_at"] = utc_now_iso()
        _persist_index()


def get_status(run_id: str) -> Dict[str, Any]:
    rec = RUNS.get(run_id)
    if not rec:
        return {"run_id": run_id, "status": "not_found"}
    return {
        "run_id": run_id,
        "status": rec.get("status"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "last_event": rec.get("last_event"),
        "results": rec.get("results"),
    }


async def execute_run(run_id: str) -> None:
    """Launch a batch run using the runner with settings/flow loaded from config.

    Keeps API compatible with main.py and persists results to jsonl + sqlite.
    """
    rec = RUNS.get(run_id)
    if not rec:
        return

    # track for cancellation
    _TASKS[run_id] = asyncio.current_task()  # type: ignore[assignment]

    # request and basic validation
    req: Dict[str, Any] = rec.get("request") or {}
    RUNS[run_id]["status"] = "running"
    RUNS[run_id]["updated_at"] = utc_now_iso()
    _persist_index()

    conc = int(req.get("concurrency") or 1)
    accounts: List[Dict[str, Any]] = list(req.get("accounts") or [])
    if not accounts:
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["updated_at"] = utc_now_iso()
        RUNS[run_id]["results"] = []
        _persist_index()
        _TASKS.pop(run_id, None)
        return

    results: List[Dict[str, Any]] = []

    # Load settings/flow
    import yaml
    settings_path = Path("config/settings.yaml")
    flow_path = Path("config/flow.yaml")
    settings: Dict[str, Any] = {}
    flow: Dict[str, Any] = {}
    try:
        if settings_path.exists():
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if flow_path.exists():
            flow = yaml.safe_load(flow_path.read_text(encoding="utf-8")) or {}
    except Exception:
        settings, flow = {}, {}

    # Runner
    from .runner import Account, RunnerConfig, run_batch

    # default concurrency from settings if not provided in request
    try:
        if not conc:
            conc = int(settings.get("concurrency") or 1)
    except Exception:
        conc = max(1, conc or 1)

    # Bridge: fetch saved accounts (for OTP + optional password reuse)
    import mail_service as ms
    try:
        saved_accounts = await ms.get_all_accounts()
    except Exception:
        saved_accounts = {}

    acc_objs: List[Account] = []
    for a in accounts:
        # default password: use accounts.json password if enabled, else settings.default_site_password
        pwd = a.get("password")
        if not pwd:
            try:
                if bool(settings.get("use_outlook_password_as_site_password", True)):
                    pwd = (saved_accounts.get(a.get("email") or "", {}) or {}).get("password")
                if not pwd:
                    pwd = settings.get("default_site_password") or None
            except Exception:
                pass
        # user agent: only honor per-account; do not override from settings for natural env
        ua = a.get("user_agent")
        acc_objs.append(
            Account(
                email=a.get("email"),
                password=pwd,
                display_name=a.get("display_name", "user"),
                dob=a.get("dob", "1999-09-09"),
                proxy=a.get("proxy"),
                user_agent=ua,
            )
        )

    # headful/headless mapping
    req_headful = bool(req.get("headful", True))
    settings_headless = bool(settings.get("headless", False))
    settings_show_window = bool(settings.get("show_window", not settings_headless))
    headless = not (req_headful or settings_show_window)

    # Prefer system Chrome when channel unspecified
    ch = settings.get("browser_channel")
    if isinstance(ch, str) and ch.strip() == "":
        ch = None
    cfg = RunnerConfig(
        concurrency=conc,
        navigation_timeout_ms=int(settings.get("navigation_timeout_ms", 60000) or 60000),
        chrome_executable=(settings.get("chrome_executable") or None),
        channel=ch or None,
        headless=headless,
        force_incognito_ui=True,
        force_cdp_incognito=False,
        pure_native=bool(settings.get("pure_native", False)),
        native_disable_automation_flag=bool(settings.get("native_disable_automation_flag", True)),
    )

    try:
        if not getattr(cfg, "chrome_executable", None) and not getattr(cfg, "channel", None):
            cfg.channel = "chrome"
    except Exception:
        pass

    async def progress_cb(evt: Dict[str, Any]):
        evt["event_id"] = uuid.uuid4().hex
        evt["timestamp"] = utc_now_iso()
        RUNS[run_id]["last_event"] = evt
        RUNS[run_id]["updated_at"] = utc_now_iso()

    try:
        if acc_objs:
            results = await run_batch(
                accounts=acc_objs,
                flow=flow,
                settings=settings,
                cfg=cfg,
                limit=req.get("limit"),
                progress_cb=progress_cb,
                should_cancel=lambda: _should_cancel(run_id),
                wait_if_paused=lambda: _wait_if_paused(run_id),
            )
        else:
            results = []
        RUNS[run_id]["status"] = "completed"
    except asyncio.CancelledError:
        RUNS[run_id]["status"] = "cancelled"
        results = RUNS[run_id].get("results", []) or []
        raise
    except Exception as e:  # noqa: BLE001
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["error"] = str(e)
        results = RUNS[run_id].get("results", []) or []
    finally:
        RUNS[run_id]["updated_at"] = utc_now_iso()
        RUNS[run_id]["results"] = results
        _persist_index()
        _TASKS.pop(run_id, None)

        _write_jsonl(run_id, results)
        try:
            for r in results:
                err = r.get("error")
                err_json = json.dumps(err, ensure_ascii=False) if err else None
                _db_execute(
                    "INSERT INTO results(run_id, email, status, attempts, duration_sec, error_json) VALUES(?,?,?,?,?,?)",
                    (
                        run_id,
                        r.get("email"),
                        r.get("status"),
                        r.get("attempts"),
                        r.get("duration_sec"),
                        err_json,
                    ),
                )
        except Exception:
            pass
