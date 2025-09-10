from __future__ import annotations

import asyncio
import os
import random
import tempfile
import shutil

from typing import Optional, Dict, Any

from playwright.async_api import async_playwright, Page


class IncognitoChrome:
    """Per-account Chrome incognito window manager using Playwright.

    - Uses launch_persistent_context with a temp user dir + --incognito + --new-window
    - Headful by default to ensure native Chrome incognito UI
    - Optional per-session overrides: proxy_url, user_agent, accept_language, locale, timezone_id
    """

    def __init__(
        self,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        chrome_executable: Optional[str] = None,
        channel: Optional[str] = "chrome",
        headless: bool = False,
        force_incognito_ui: bool = True,
        force_cdp_incognito: bool = False,
        navigation_timeout_ms: int = 45000,
        record_video_dir: Optional[str] = None,
        pure_native: bool = False,
        relax_fingerprints: bool = True,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.locale = locale
        self.timezone_id = timezone_id
        self.chrome_executable = chrome_executable
        self.channel = channel
        self.headless = headless
        self.force_incognito_ui = force_incognito_ui
        self.force_cdp_incognito = force_cdp_incognito
        self.navigation_timeout_ms = navigation_timeout_ms
        self.record_video_dir = record_video_dir
        self.pure_native = pure_native
        self.relax_fingerprints = relax_fingerprints
        self.extra_args = extra_args or []

        self._pw = None
        self._browser = None
        self._ctx = None
        self._user_data_dir = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        # 使用持久化上下文，按 pure_native 最小化参数控制
        args: list[str] = []
        # 始终使用无痕新窗口（满足“黑帽无痕窗口”）
        args.extend(["--new-window", "--incognito"])  # 仅最小必要参数
        if not self.pure_native:
            # 非纯原生时可加入常见测试参数（尽量保守）
            args.extend([
                "--no-first-run",
                "--no-default-browser-check",
                # "--disable-infobars",  # 避免明显自动化指示
                # 仅在确有需要时才考虑允许第三方 Cookie，默认保持浏览器原生策略
                # "--disable-features=BlockThirdPartyCookies",
            ])
            if self.relax_fingerprints:
                # 注意：部分站点会识别该开关；仅在 relax_fingerprints 为真时启用
                args.append("--disable-blink-features=AutomationControlled")
        if self.extra_args:
            try:
                for a in self.extra_args:
                    if isinstance(a, str) and a.strip():
                        args.append(a.strip())
            except Exception:
                pass
        ctx_kwargs: Dict[str, Any] = dict(
            headless=bool(self.headless),
            args=args,
            proxy=({"server": self.proxy_url} if self.proxy_url else None),
            user_agent=self.user_agent,
            locale=self.locale,
            timezone_id=self.timezone_id,
        )
        # 仅在“非纯原生 + 放宽指纹”模式下移除 --enable-automation
        if not self.pure_native and self.relax_fingerprints:
            ctx_kwargs["ignore_default_args"] = ["--enable-automation"]
        if self.record_video_dir:
            try:
                os.makedirs(self.record_video_dir, exist_ok=True)
                ctx_kwargs["record_video_dir"] = self.record_video_dir
            except Exception:
                pass
        if self.chrome_executable and os.path.exists(self.chrome_executable):
            ctx_kwargs["executable_path"] = self.chrome_executable
        elif self.channel:
            # 仅允许 'chrome' 渠道，确保黑色匿名帽子 UI
            ctx_kwargs["channel"] = self.channel

        # 使用 launch_persistent_context 以获得真正的无痕窗口 UI
        self._user_data_dir = tempfile.mkdtemp(prefix="ag-incog-")
        self._ctx = await self._pw.chromium.launch_persistent_context(self._user_data_dir, **ctx_kwargs)

        # do not inject headers or override navigator/Intl; keep environment natural
        self._ctx.set_default_timeout(self.navigation_timeout_ms)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            user_dir = getattr(self, "_user_data_dir", None)
            if user_dir and os.path.isdir(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def new_page(self) -> Page:
        if not self._ctx:
            raise RuntimeError("Context not initialized")
        if self._ctx.pages:
            return self._ctx.pages[0]
        return await self._ctx.new_page()

    # Convenience helpers
    async def goto(self, page: Page, url: str, *, wait: str = "domcontentloaded"):
        await page.goto(url, wait_until=wait)
        await asyncio.sleep(0.2)

    async def click(self, page: Page, selector: str):
        await page.locator(selector).first.click()
        await asyncio.sleep(0.1)

    async def type(self, page: Page, selector: str, text: str, *, delay_ms: int = 80):
        await page.locator(selector).first.fill("")
        await page.locator(selector).first.type(text, delay=delay_ms)
        await asyncio.sleep(0.1)


class PureChrome:
    """Pure Playwright defaults: launch Chrome with no extra flags and a fresh context.

    - No custom args, no header/UA/timezone overrides
    - Headful/Headless toggled by parameter
    """

    def __init__(
        self,
        *,
        chrome_executable: Optional[str] = None,
        channel: Optional[str] = "chrome",
        headless: bool = False,
        navigation_timeout_ms: int = 45000,
        disable_automation_flag: bool = False,
    ) -> None:
        self.chrome_executable = chrome_executable
        self.channel = channel
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.disable_automation_flag = disable_automation_flag
        self._pw = None
        self._browser = None
        self._ctx = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        launch_kwargs: Dict[str, Any] = dict(headless=bool(self.headless))
        if self.chrome_executable and os.path.exists(self.chrome_executable):
            launch_kwargs["executable_path"] = self.chrome_executable
        elif self.channel:
            launch_kwargs["channel"] = self.channel
        if self.disable_automation_flag:
            launch_kwargs["ignore_default_args"] = ["--enable-automation"]

        # Add args for better auth compatibility (similar to IncognitoChrome)
        launch_kwargs["args"] = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-features=BlockThirdPartyCookies",  # Allow third-party cookies for auth
        ]

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._ctx = await self._browser.new_context()
        self._ctx.set_default_timeout(self.navigation_timeout_ms)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def new_page(self) -> Page:
        if not self._ctx:
            raise RuntimeError("Context not initialized")
        if self._ctx.pages:
            return self._ctx.pages[0]
        return await self._ctx.new_page()
