from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import time


def utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StepError(Exception):
    """Structured flow error for consistent reporting.

    Carries the failing step, optional current URL and a best-effort screenshot
    name saved under `run-logs`. Runners can serialize via `to_dict()`.
    """

    def __init__(
        self,
        step: str,
        message: str,
        *,
        url: str | None = None,
        page=None,
        email: str | None = None,
        error_type: str = "error",
    ) -> None:
        super().__init__(message)
        self.step = step
        self.message = message
        self.url = url or ""
        self.error_type = error_type
        self.screenshot: Optional[str] = None

        # Best-effort screenshot into run-logs to aid debugging
        if page is not None:
            try:
                current_url = getattr(page, "url", "") or ""
            except Exception:
                current_url = ""
            if not self.url:
                self.url = current_url
            try:
                Path("run-logs").mkdir(exist_ok=True)
                safe_email = (email or "unknown").replace("/", "_").replace("\\", "_")
                fname = f"failure_{safe_email}_{step}_{int(time.time())}.png"
                out = Path("run-logs") / fname
                page.screenshot(path=str(out))
                self.screenshot = out.name
            except Exception:
                self.screenshot = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "step": self.step,
            "url": self.url,
            "error_type": self.error_type,
            "screenshot": self.screenshot,
        }


# Small helpers for retry/backoff access from settings
def get_nested(d: Dict[str, Any] | None, *keys: str, default=None):
    cur: Any = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

