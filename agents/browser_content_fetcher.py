#!/usr/bin/env python3
"""
Browser-based page content fetch utility.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from local_browser_mcp import LocalBrowserMCP


_LOCAL_BROWSER_MCP = LocalBrowserMCP()

# When set (by agent runtime), Playwright fetch is limited to this URL only.
_BROWSER_FETCH_PIN: ContextVar[str | None] = ContextVar("browser_fetch_pin", default=None)


def canonical_browser_policy_url(url: str) -> str:
    """Normalize URL for equality checks (trim, strip trailing slashes)."""
    u = (url or "").strip()
    while len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u


def browser_urls_match(a: str, b: str) -> bool:
    return canonical_browser_policy_url(a) == canonical_browser_policy_url(b)


@contextmanager
def pinned_browser_fetch_url(url: str | None):
    """Restrict fetch_rendered_page_content to exactly this http(s) URL while active."""
    u = (url or "").strip()
    if not u:
        yield
        return
    tok = _BROWSER_FETCH_PIN.set(u)
    try:
        yield
    finally:
        _BROWSER_FETCH_PIN.reset(tok)


def fetch_rendered_page_content(
    url: str,
    *,
    wait_until: str = "networkidle",
    timeout_ms: int = 15000,
    post_wait_ms: int = 1000,
    max_chars: int = 50000,
    headless: bool = False,
) -> str:
    """
    Backward-compatible wrapper over local Browser MCP runtime.
    """
    del wait_until  # Kept for compatibility with previous call sites.
    if timeout_ms <= 0:
        timeout_ms = 15000
    if post_wait_ms < 0:
        post_wait_ms = 0
    if max_chars <= 0:
        max_chars = 50000
    pinned = _BROWSER_FETCH_PIN.get()
    if pinned:
        parsed = urlparse((url or "").strip())
        if parsed.scheme not in {"http", "https"} or not browser_urls_match(url, pinned):
            return (
                "Error: browser_mcp 已锁定为仅抓取用户 reference 中的页面；"
                f"不允许请求 URL：{(url or '').strip()!r}。请使用 reference 中的完整链接。"
            )
    try:
        return _LOCAL_BROWSER_MCP.fetch_topic_content(
            url=url,
            timeout_ms=timeout_ms,
            post_wait_ms=post_wait_ms,
            max_chars=max_chars,
            headless=headless,
        )
    except Exception as e:
        return (
            "Browser MCP fetch error. Ensure Playwright runtime is ready "
            "(`pip install playwright` and `playwright install`). "
            f"Details: {e}"
        )
