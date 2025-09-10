from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit

from playwright.async_api import Page

from gpt_register.core import StepError
from gpt_register.otp import OtpConfig, poll_for_code
from . import register as register_flow


async def _idle(min_s: float = 0.15, max_s: float = 0.35) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _natural_page_behavior(page: Page, duration_s: float) -> None:
    """在给定时长内模拟滚动、鼠标移动与短暂停顿，提升观察期自然度。"""
    end = asyncio.get_event_loop().time() + max(0.5, float(duration_s))
    while asyncio.get_event_loop().time() < end:
        action = random.choice(["scroll", "move", "pause"]) 
        try:
            if action == "scroll":
                await page.mouse.wheel(0, random.randint(-120, 240))
            elif action == "move":
                vp = await page.viewport_size()
                if vp:
                    x = random.randint(20, max(21, vp["width"] - 20))
                    y = random.randint(20, max(21, vp["height"] - 20))
                    await page.mouse.move(x, y, steps=random.randint(4, 10))
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.25, 0.8))


def _host(url: str) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except Exception:
        return ""


async def _click_human(page: Page, selector: str) -> None:
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=20000)
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    box = await loc.bounding_box()
    if box:
        tx = box["x"] + box["width"] * random.uniform(0.45, 0.65)
        ty = box["y"] + box["height"] * random.uniform(0.45, 0.65)
        await page.mouse.move(tx + random.uniform(-4, 4), ty + random.uniform(-3, 3), steps=random.randint(8, 16))
        await _idle(0.05, 0.18)
    else:
        await _idle(0.05, 0.12)
    try:
        await loc.hover()
    except Exception:
        pass
    await _idle(0.04, 0.10)
    await loc.click()


async def _fill_text(page: Page, selector: str, text: str) -> None:
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=25000)
    try:
        await loc.click()
        await _idle(0.05, 0.12)
    except Exception:
        pass
    await loc.fill("")
    await loc.type(text, delay=random.randint(45, 95))
    await _idle(0.05, 0.12)


async def _action_pause(settings: Dict[str, Any], *, base_ms: int = 200) -> None:
    try:
        ms = int((settings or {}).get("action_delay_ms", base_ms) or base_ms)
    except Exception:
        ms = base_ms
    try:
        j = int((settings or {}).get("action_delay_jitter_ms", 300) or 0)
    except Exception:
        j = 0
    await asyncio.sleep(max(0, ms + random.randint(-j, j)) / 1000.0)


async def _page_contains_cf_challenge(page: Page) -> bool:
    try:
        if await page.locator("iframe[src*='challenges.cloudflare.com']").first.is_visible(timeout=200):
            return True
    except Exception:
        pass
    for t in ["Verify you are human", "确认您是真人", "请确认您是真人", "Cloudflare"]:
        try:
            if await page.get_by_text(t, exact=False).first.is_visible(timeout=200):
                return True
        except Exception:
            continue
    return False


async def _wait_cloudflare_if_any(page: Page, settings: Dict[str, Any]) -> None:
    cf = (settings or {}).get("cloudflare") or {}
    max_wait = float(cf.get("max_wait_sec", 90) or 90)
    poll_ms = int(cf.get("poll_interval_ms", 750) or 750)
    deadline = asyncio.get_event_loop().time() + max_wait
    seen = False
    while asyncio.get_event_loop().time() < deadline:
        if await _page_contains_cf_challenge(page):
            seen = True
            await asyncio.sleep(max(0.2, poll_ms / 1000))
            continue
        if seen:
            await _idle(0.25, 0.6)
        break


async def _ensure_allowed_host(page: Page, allowed: Iterable[str]) -> None:
    cur = _host(getattr(page, "url", ""))
    if cur and cur.lower() in {h.lower() for h in allowed}:
        return
    for _ in range(20):
        await asyncio.sleep(0.15)
        cur = _host(getattr(page, "url", ""))
        if cur and cur.lower() in {h.lower() for h in allowed}:
            return


async def _risk_guard(page: Page, *, email: str) -> None:
    cur = getattr(page, "url", "") or ""
    h = _host(cur)
    if h.endswith("platform.openai.com"):
        msg = "检测到 OpenAI 风控页面"
        try:
            if await page.get_by_text("糟糕", exact=False).first.is_visible(timeout=300):
                msg = "检测到 OpenAI 风控页面: 糟糕！We ran into an issue"
        except Exception:
            pass
        raise StepError("risk", msg, page=page, email=email, error_type="business")


async def run(page: Page, *, account: Any, settings: Dict[str, Any]):
    """更自然的 ChatGPT 注册流程（仅一次初始导航，无硬跳；中文页面兼容）。"""

    email = getattr(account, "email", None) or ""
    if not email:
        raise StepError("precheck", "account.email is required")

    pwd_suffix = getattr(account, "password", None) or ""
    site_pwd = f"gptteam123456789{pwd_suffix}"

    otp_raw = (settings.get("otp") if isinstance(settings, dict) else {}) or {}
    otp_cfg = OtpConfig(
        subject_contains=otp_raw.get("subject_contains") or "OpenAI",
        code_regex=otp_raw.get("code_regex") or r"\b(\d{6})\b",
        poll_interval_sec=int(otp_raw.get("poll_interval_sec") or 3),
        poll_timeout_sec=int(otp_raw.get("poll_timeout_sec") or 180),
        from_contains=(otp_raw.get("from_contains") or None) or None,
        junk_first=bool(otp_raw.get("junk_first") if otp_raw.get("junk_first") is not None else True),
        max_age_min=int(otp_raw.get("max_age_min") or 15),
    )

    # 1) 仅一次硬导航：chatgpt.com
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    try:
        home_budget = float(((settings or {}).get("home_budget_sec") or 6.0) or 6.0)
    except Exception:
        home_budget = 6.0
    await _natural_page_behavior(page, home_budget)
    await _wait_cloudflare_if_any(page, settings)
    await _risk_guard(page, email=email)

    # 2) 点击“免费注册/Sign up”
    await _idle(1.0, 2.0)
    try:
        try:
            await _click_human(page, "[data-testid='signup-button']")
        except Exception:
            await _click_human(page, "button:has-text('Sign up'), a:has-text('Sign up'), button:has-text('免费注册'), a:has-text('免费注册')")
    except Exception as e:
        raise StepError("click_signup", "点击 Sign up/免费注册 失败", page=page, email=email) from e

    await _action_pause(settings)
    await _wait_cloudflare_if_any(page, settings)
    await _risk_guard(page, email=email)
    try:
        await _ensure_allowed_host(page, ["chatgpt.com", "auth.openai.com"])  # OIDC 常见宿主
    except Exception:
        pass

    # 3) 先尝试直接填写邮箱；如果没有输入框再点“使用邮箱继续”后填写
    email_input_sel = "input[type='email'], input[autocomplete='username'], input[name='email'], input[name='username']"
    try:
        if await page.locator(email_input_sel).first.is_visible(timeout=800):
            await _fill_text(page, email_input_sel, email)
        else:
            raise RuntimeError("email_input_not_visible")
    except Exception:
        try:
            cont_sel_list = [
                "button:has-text('Continue with email')",
                "button:has-text('Continue')",
                "button:has-text('使用电子邮件继续')",
                "button:has-text('用邮箱继续')",
                "button:has-text('使用邮箱继续')",
            ]
            await _click_human(page, ", ".join(cont_sel_list))
        except Exception:
            pass
        await _fill_text(page, email_input_sel, email)

    await _action_pause(settings)
    await _risk_guard(page, email=email)
    try:
        await _ensure_allowed_host(page, ["chatgpt.com", "auth.openai.com"])  # 避免在异常域上继续
    except Exception:
        pass

    # 4) 提交（中英兼容）
    try:
        await _click_human(page, "button[type='submit'], button:has-text('Continue'), button:has-text('继续'), button:has-text('下一步')")
    except Exception as e:
        raise StepError("enter_email", "邮箱填写或继续失败", page=page, email=email) from e

    # 5) 设置密码并继续
    try:
        await _fill_text(page, "input[type='password']", site_pwd)
        await _action_pause(settings)
        await _click_human(page, "button[type='submit'], button:has-text('Continue'), button:has-text('继续'), button:has-text('下一步')")
    except Exception as e:
        raise StepError("enter_password", "设置密码或继续失败", page=page, email=email) from e

    # 6) OTP 邮件验证码
    try:
        code = await poll_for_code(email, otp_cfg)
        await _fill_text(page, "input[inputmode='numeric'], input[name='code'], input[type='text']", code)
        await _action_pause(settings)
        await _click_human(page, "button[type='submit'], button:has-text('Continue'), button:has-text('继续'), button:has-text('完成')")
    except Exception as e:
        raise StepError("otp", f"OTP 失败: {e}", page=page, email=email) from e

    try:
        await asyncio.sleep(0.3)
        await page.close()
    except Exception:
        pass


try:
    register_flow("chatgpt_signup", run)
    register_flow("chatgpt", run)
except Exception:
    pass

