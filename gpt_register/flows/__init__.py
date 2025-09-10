from __future__ import annotations

from typing import Callable, Awaitable, Dict, Any

# Flow registry: name -> async run(page, account, settings)
_REGISTRY: dict[str, Callable[..., Awaitable[None]]] = {}


def register(name: str, fn: Callable[..., Awaitable[None]]) -> None:
    _REGISTRY[name.strip().lower()] = fn


def get(name: str) -> Callable[..., Awaitable[None]] | None:
    return _REGISTRY.get((name or "").strip().lower())


# Import built-in flows to auto-register
try:
    from . import chatgpt_signup  # noqa: F401
except Exception:
    # Allow running without optional deps during packaging
    pass

