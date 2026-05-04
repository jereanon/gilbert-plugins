"""BrowserService — owns the Playwright instance, the per-user context
pool, the credential store, and (later) the VNC session manager.

Implements ``Service`` and ``ToolProvider``. The plugin's ``setup()``
registers a single instance with the ServiceManager.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolResult
from gilbert.interfaces.workspace import WorkspaceProvider

from .context_pool import ContextPool
from .tools import INTERACTION_TOOLS, READ_ONLY_TOOLS

logger = logging.getLogger(__name__)

_TEXT_TRUNCATE = 50_000
_HTML_TRUNCATE = 200_000
_NAV_TIMEOUT_MS = 30_000
_INTERACT_TIMEOUT_MS = 15_000

_WHITESPACE_RUN = re.compile(r"\s+")


class BrowserService(Service):
    slash_namespace = "browser"
    tool_provider_name = "browser"

    def __init__(self, *, data_dir: Path, storage: Any) -> None:
        self._data_dir = data_dir
        self._storage = storage
        self._pw_cm: Any | None = None
        self._pw: Any | None = None
        self._pool: ContextPool | None = None
        # Per-user "default" Page handle, lazily created on the first
        # tool invocation. The ``Page`` shares its parent BrowserContext
        # with any other pages opened explicitly.
        self._pages: dict[str, Any] = {}
        # Per-user lock so concurrent tool calls in the same AI turn
        # don't race on the same Page (Playwright is not thread-safe at
        # the page level).
        self._page_locks: dict[str, asyncio.Lock] = {}
        # Resolved capabilities, set in start().
        self._workspace: WorkspaceProvider | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="browser",
            capabilities=frozenset({"browser", "ai_tools"}),
            requires=frozenset(),
            optional=frozenset({"workspace", "configuration", "ai_chat"}),
            ai_calls=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        ws = resolver.get_capability("workspace")
        if ws is not None and not isinstance(ws, WorkspaceProvider):
            logger.warning("workspace service does not implement WorkspaceProvider")
            ws = None
        self._workspace = ws

        self._pw_cm = async_playwright()
        try:
            self._pw = await self._pw_cm.start()
        except Exception:
            logger.exception(
                "Playwright failed to start. Did you run "
                "`uv run playwright install chromium`?"
            )
            self._pw_cm = None
            self._pw = None
            return
        self._pool = ContextPool(
            data_dir=self._data_dir,
            playwright=self._pw,
            idle_timeout_seconds=600,
        )
        try:
            await self._pool.start()
        except Exception:
            logger.exception(
                "Failed to launch headless Chromium. Did you run "
                "`uv run playwright install chromium`?"
            )
            self._pool = None

    async def stop(self) -> None:
        # Close any per-user Page handles before tearing down contexts.
        for user_id, page in list(self._pages.items()):
            try:
                await page.close()
            except Exception:
                logger.exception("page close failed for %s", user_id)
        self._pages.clear()
        self._page_locks.clear()
        if self._pool is not None:
            await self._pool.stop()
            self._pool = None
        if self._pw_cm is not None:
            try:
                await self._pw_cm.stop()
            except Exception:
                logger.exception("playwright stop failed")
            self._pw_cm = None
            self._pw = None

    # ------------------------------------------------------------------
    # ToolProvider
    # ------------------------------------------------------------------

    def get_tools(self, user_ctx: Any | None = None) -> list[ToolDefinition]:
        return list(READ_ONLY_TOOLS) + list(INTERACTION_TOOLS)

    async def execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> str | ToolResult:
        user_id = str(arguments.get("_user_id") or "")
        if not user_id:
            return "error: missing user context (browser tools require a user_id)"

        if self._pool is None:
            return (
                "error: browser service not initialized — Chromium may not "
                "be installed. Run `uv run playwright install chromium`."
            )

        lock = self._page_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                if name == "browser_navigate":
                    return await self._tool_navigate(user_id, arguments)
                if name == "browser_get_text":
                    return await self._tool_get_text(user_id, arguments)
                if name == "browser_get_html":
                    return await self._tool_get_html(user_id, arguments)
                if name == "browser_screenshot":
                    return await self._tool_screenshot(user_id, arguments)
                if name == "browser_click":
                    return await self._tool_click(user_id, arguments)
                if name == "browser_fill":
                    return await self._tool_fill(user_id, arguments)
                if name == "browser_press":
                    return await self._tool_press(user_id, arguments)
                if name == "browser_select":
                    return await self._tool_select(user_id, arguments)
            except Exception as exc:
                logger.exception("browser tool %s failed", name)
                return f"error: {type(exc).__name__}: {exc}"
        raise KeyError(name)

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _get_or_create_page(self, user_id: str) -> Any:
        page = self._pages.get(user_id)
        if page is None:
            assert self._pool is not None
            ctx = await self._pool.get_for_user(user_id)
            page = await ctx.new_page()
            self._pages[user_id] = page
        return page

    async def _tool_navigate(self, user_id: str, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "error: url required"
        if "://" not in url:
            return f"error: url must include scheme (got '{url}')"
        page = await self._get_or_create_page(user_id)
        await page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
        title = await page.title()
        return f"Loaded {page.url} — {title}"

    async def _tool_get_text(self, user_id: str, args: dict[str, Any]) -> str:
        selector = str(args.get("selector", "")).strip()
        page = await self._get_or_create_page(user_id)
        target = page.locator(selector) if selector else page.locator("body")
        text = await target.inner_text()
        text = _WHITESPACE_RUN.sub(" ", text or "").strip()
        if len(text) > _TEXT_TRUNCATE:
            text = text[:_TEXT_TRUNCATE] + " …[truncated]"
        return text

    async def _tool_get_html(self, user_id: str, args: dict[str, Any]) -> str:
        selector = str(args.get("selector", "")).strip()
        page = await self._get_or_create_page(user_id)
        if selector:
            html = await page.locator(selector).inner_html()
        else:
            html = await page.content()
        if len(html) > _HTML_TRUNCATE:
            html = html[:_HTML_TRUNCATE] + " <!--[truncated]-->"
        return html

    async def _tool_screenshot(
        self, user_id: str, args: dict[str, Any]
    ) -> ToolResult | str:
        if self._workspace is None:
            return ToolResult(
                tool_call_id="",
                content="error: workspace service unavailable; cannot persist screenshot",
                is_error=True,
            )
        conv_id = str(args.get("_conversation_id") or "")
        if not conv_id:
            return ToolResult(
                tool_call_id="",
                content="error: screenshot requires a conversation context",
                is_error=True,
            )

        full_page = bool(args.get("full_page", False))
        page = await self._get_or_create_page(user_id)
        png = await page.screenshot(full_page=full_page, type="png")

        out_dir: Path = self._workspace.get_output_dir(user_id, conv_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self._unique_name(out_dir, f"browser-{int(time.time())}.png")
        dest = out_dir / name
        dest.write_bytes(png)

        entity = await self._workspace.register_file(
            conversation_id=conv_id,
            user_id=user_id,
            category="output",
            filename=name,
            rel_path=f"outputs/{name}",
            media_type="image/png",
            size=len(png),
            created_by="ai",
        )

        attachment = FileAttachment(
            kind="image",
            name=name,
            media_type="image/png",
            workspace_skill="workspace",
            workspace_path=f"outputs/{name}",
            workspace_conv=conv_id,
            workspace_file_id=entity.get("_id", ""),
            size=len(png),
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Captured screenshot ({len(png)} bytes). The user will "
                f"see it inline in your reply."
            ),
            attachments=(attachment,),
        )

    async def _tool_click(self, user_id: str, args: dict[str, Any]) -> str:
        selector = str(args.get("selector", "")).strip()
        if not selector:
            return "error: selector required"
        page = await self._get_or_create_page(user_id)
        await page.locator(selector).click(timeout=_INTERACT_TIMEOUT_MS)
        return f"Clicked {selector}"

    async def _tool_fill(self, user_id: str, args: dict[str, Any]) -> str:
        selector = str(args.get("selector", "")).strip()
        value = str(args.get("value", ""))
        if not selector:
            return "error: selector required"
        page = await self._get_or_create_page(user_id)
        await page.locator(selector).fill(value, timeout=_INTERACT_TIMEOUT_MS)
        return f"Filled {selector}"

    async def _tool_press(self, user_id: str, args: dict[str, Any]) -> str:
        key = str(args.get("key", "")).strip()
        if not key:
            return "error: key required"
        page = await self._get_or_create_page(user_id)
        await page.keyboard.press(key)
        return f"Pressed {key}"

    async def _tool_select(self, user_id: str, args: dict[str, Any]) -> str:
        selector = str(args.get("selector", "")).strip()
        value = str(args.get("value", ""))
        if not selector:
            return "error: selector required"
        page = await self._get_or_create_page(user_id)
        await page.locator(selector).select_option(value, timeout=_INTERACT_TIMEOUT_MS)
        return f"Selected {value} in {selector}"

    @staticmethod
    def _unique_name(out_dir: Path, base: str) -> str:
        if not (out_dir / base).exists():
            return base
        stem, _, ext = base.rpartition(".")
        if not stem:
            stem, ext = base, ""
        for i in range(1, 1000):
            candidate = f"{stem}-{i}.{ext}" if ext else f"{stem}-{i}"
            if not (out_dir / candidate).exists():
                return candidate
        return base
