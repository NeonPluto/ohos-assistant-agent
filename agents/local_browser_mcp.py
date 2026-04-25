#!/usr/bin/env python3
"""
Local Browser MCP-like runtime for agent usage.
"""

from __future__ import annotations

from urllib.parse import urlparse


class LocalBrowserMCP:
    """
    A lightweight local browser runtime exposing MCP-like actions.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must start with http:// or https:// and include a host")
        return url.strip()

    def _ensure_session(self, *, headless: bool = True) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def navigate(self, url: str, *, timeout_ms: int = 15000, wait_until: str = "domcontentloaded", headless: bool = True) -> str:
        safe_url = self._validate_url(url)
        self._ensure_session(headless=headless)
        self._page.goto(safe_url, wait_until=wait_until, timeout=timeout_ms)
        return f"navigated: {safe_url}"

    def wait_ready(self, *, timeout_ms: int = 15000, post_wait_ms: int = 1000) -> str:
        if self._page is None:
            raise RuntimeError("browser session is not initialized; call navigate first")
        self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass
        try:
            self._page.locator("body").first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            pass

        previous_len = -1
        stable_rounds = 0
        for _ in range(8):
            try:
                current_len = self._page.evaluate(
                    """() => {
                        const el = document.body;
                        if (!el) return 0;
                        return (el.innerText || "").trim().length;
                    }"""
                )
            except Exception:
                current_len = 0
            if current_len > 0 and current_len == previous_len:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 2:
                break
            previous_len = current_len
            self._page.wait_for_timeout(300)

        if post_wait_ms > 0:
            self._page.wait_for_timeout(post_wait_ms)
        return "ready"

    def snapshot_text(self, *, timeout_ms: int = 15000, max_chars: int = 50000) -> str:
        if self._page is None:
            raise RuntimeError("browser session is not initialized; call navigate first")
        selectors = [
            "main",
            "article",
            "[role='main']",
            ".content",
            ".article",
            ".post",
            "#content",
            "#main",
            "body",
        ]
        text = ""
        for selector in selectors:
            try:
                candidate = self._page.locator(selector).first.inner_text(timeout=timeout_ms)
                cleaned = " ".join((candidate or "").split())
                if len(cleaned) >= 80:
                    text = cleaned
                    break
                if not text and cleaned:
                    text = cleaned
            except Exception:
                continue
        return text[:max_chars] if text else "(page body is empty)"

    def fetch_topic_content(
        self,
        url: str,
        *,
        timeout_ms: int = 15000,
        post_wait_ms: int = 1000,
        max_chars: int = 50000,
        headless: bool = True,
    ) -> str:
        self.navigate(url, timeout_ms=timeout_ms, wait_until="domcontentloaded", headless=headless)
        self.wait_ready(timeout_ms=timeout_ms, post_wait_ms=post_wait_ms)
        return self.snapshot_text(timeout_ms=timeout_ms, max_chars=max_chars)

