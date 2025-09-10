from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Callable, Awaitable

from playwright.async_api import Page

from .browser import IncognitoChrome, PureChrome
from .flows import get as get_flow


@dataclass
class Account:
    email: str
    password: str | None = None
    display_name: str = "user"
    dob: str = "1999-09-09"
    proxy: str | None = None
    user_agent: str | None = None


@dataclass
class RunnerConfig:
    concurrency: int = 1
    navigation_timeout_ms: int = 45000
    chrome_executable: str | None = None
    channel: str | None = "chrome"
    headless: bool = False
    force_incognito_ui: bool = True
    force_cdp_incognito: bool = False
    pure_native: bool = False
    native_disable_automation_flag: bool = False


async def _run_steps(page: Page, steps: List[Dict[str, Any]]) -> None:
    """Very small step runner: supports goto/click/type/wait_for.
    This is placeholder logic; expand as needed when wiring real flow.
    """
    for st in steps or []:
        name = (st.get("name") or st.get("type") or "").lower()
        if name == "goto":
            url = st.get("url")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(0.2)
        elif name == "click":
            sel = st.get("selector")
            await page.locator(sel).first.click()
            await asyncio.sleep(0.2)
        elif name == "type":
            sel = st.get("selector"); txt = st.get("text", "")
            await page.locator(sel).first.fill("")
            await page.locator(sel).first.type(txt, delay=60)
        elif name == "wait_for":
            sel = st.get("selector"); state = st.get("state", "visible")
            await page.wait_for_selector(sel, state=state)
        else:
            # ignore unknown
            await asyncio.sleep(0)


async def process_account(
    *,
    account: Account,
    flow: Dict[str, Any],
    settings: Dict[str, Any],
    cfg: RunnerConfig,
    progress_cb: Callable[[Dict[str, Any]], Awaitable[None]] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    wait_if_paused: Callable[[], Awaitable[None]] | None = None,
) -> Dict[str, Any]:
    start = time.time()
    if progress_cb:
        try:
            await progress_cb({"type": "start", "email": account.email})
        except Exception:
            pass
    try:
        if should_cancel and await should_cancel():
            raise RuntimeError("run_cancelled")
        if wait_if_paused:
            await wait_if_paused()

        # 固定使用 IncognitoChrome（黑帽无痕 UI），并可按 settings 放宽指纹痕迹
        # 注意：我们不自动绕过任何人机验证，仅减少自动化表征
        extra_args = []
        try:
            v = (settings or {}).get("browser_extra_args")
            if isinstance(v, list):
                extra_args = [str(x) for x in v if isinstance(x, (str, int, float))]
        except Exception:
            extra_args = []

        ctx_mgr = IncognitoChrome(
            proxy_url=account.proxy,
            user_agent=account.user_agent,
            # keep environment simple: do not override headers/locale/timezone
            accept_language=None,
            locale=None,
            timezone_id=None,
            chrome_executable=cfg.chrome_executable,
            channel=cfg.channel,
            headless=cfg.headless,
            force_incognito_ui=cfg.force_incognito_ui,
            force_cdp_incognito=cfg.force_cdp_incognito,
            navigation_timeout_ms=cfg.navigation_timeout_ms,
            pure_native=cfg.pure_native,
            relax_fingerprints=bool((settings or {}).get("relax_fingerprints", True)),
            extra_args=extra_args,
        )
        async with ctx_mgr as chrome:
            page = await chrome.new_page()
            # 尝试在失败时短暂保留窗口，便于观察
            hold_on_error_sec = 0.0
            try:
                hold_on_error_sec = float(((settings or {}).get("debug_hold_on_error_sec") or 0) or 0)
            except Exception:
                hold_on_error_sec = 0.0
            try:
                # resolve preset via flow registry, fallback to simple step runner
                preset = (flow or {}).get("preset") or ""
                fn = get_flow(str(preset)) if preset else None
                if fn:
                    await fn(page, account=account, settings=settings)
                else:
                    start_url = (flow or {}).get("start_url") or "https://example.com"
                    await page.goto(start_url, wait_until="domcontentloaded")
                    # optional steps
                    await _run_steps(page, list((flow or {}).get("steps") or []))
            except Exception:
                # 如果配置了 debug_hold_on_error_sec，则在退出前停留
                if hold_on_error_sec > 0:
                    try:
                        await asyncio.sleep(min(hold_on_error_sec, 10.0))
                    except Exception:
                        pass
                raise

        dur = round(time.time() - start, 2)
        res = {"email": account.email, "status": "success", "attempts": 1, "duration_sec": dur}
        if progress_cb:
            try:
                await progress_cb({"type": "done", "email": account.email, "result": res})
            except Exception:
                pass
        return res
    except Exception as e:  # noqa: BLE001
        if str(e) == "run_cancelled":
            return {"email": account.email, "status": "cancelled", "attempts": 0, "duration_sec": 0}
        dur = round(time.time() - start, 2)
        # Enrich error payload if the exception exposes details (from flow)
        err: Dict[str, Any] = {"message": str(e)}
        try:
            # flow StepError provides to_dict()
            to_dict = getattr(e, "to_dict", None)
            if callable(to_dict):
                err = to_dict()
        except Exception:
            pass
        res = {"email": account.email, "status": "error", "error": err, "attempts": 1, "duration_sec": dur}
        if progress_cb:
            try:
                await progress_cb({"type": "error", "email": account.email, "error": err})
            except Exception:
                pass
        return res


async def run_batch(
    *,
    accounts: List[Account],
    flow: Dict[str, Any],
    settings: Dict[str, Any],
    cfg: RunnerConfig,
    limit: int | None = None,
    progress_cb: Callable[[Dict[str, Any]], Awaitable[None]] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    wait_if_paused: Callable[[], Awaitable[None]] | None = None,
) -> List[Dict[str, Any]]:
    if limit is not None:
        accounts = accounts[:limit]
    # base concurrency
    conc = max(1, int(getattr(cfg, "concurrency", 1) or 1))

    # Apply simple risk throttling: if no proxy for any account, cap by settings.risk.max_concurrency_per_ip
    try:
        risk = (settings or {}).get("risk") or {}
        cap = int(risk.get("max_concurrency_per_ip", 1) or 1)
        if cap > 0 and not any(getattr(a, "proxy", None) for a in accounts):
            conc = max(1, min(conc, cap))
    except Exception:
        pass

    # Optional staggered starts to avoid burst detection
    try:
        risk = (settings or {}).get("risk") or {}
        stagger = float(risk.get("stagger_start_sec", 0.0) or 0.0)
    except Exception:
        stagger = 0.0
    results: List[Dict[str, Any]] = []
    if conc == 1:
        for i, acc in enumerate(accounts):
            if should_cancel and await should_cancel():
                results.append({"email": acc.email, "status": "cancelled"})
                break
            if wait_if_paused:
                await wait_if_paused()
            if stagger and i:
                await asyncio.sleep(stagger)
            r = await process_account(
                account=acc, flow=flow, settings=settings, cfg=cfg,
                progress_cb=progress_cb, should_cancel=should_cancel, wait_if_paused=wait_if_paused,
            )
            results.append(r)
        return results

    sem = asyncio.Semaphore(conc)

    async def _one(acc: Account, idx: int) -> Dict[str, Any]:
        async with sem:
            if should_cancel and await should_cancel():
                return {"email": acc.email, "status": "cancelled"}
            if wait_if_paused:
                await wait_if_paused()
            if stagger and idx:
                await asyncio.sleep(stagger * idx)
            return await process_account(
                account=acc, flow=flow, settings=settings, cfg=cfg,
                progress_cb=progress_cb, should_cancel=should_cancel, wait_if_paused=wait_if_paused,
            )

    return await asyncio.gather(*[_one(a, i) for i, a in enumerate(accounts)])
