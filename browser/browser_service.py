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

from gilbert.interfaces.ai import Message, MessageRole
from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolParameterType, ToolResult
from gilbert.interfaces.workspace import WorkspaceProvider

from .context_pool import ContextPool
from .credentials import BrowserCredential, CredentialStore
from .login_runner import LoginRunner
from .tools import INTERACTION_TOOLS, READ_ONLY_TOOLS, SMART_TOOLS
from .vnc import VncSessionManager

logger = logging.getLogger(__name__)

_TEXT_TRUNCATE = 50_000
_HTML_TRUNCATE = 200_000
_NAV_TIMEOUT_MS = 30_000
_INTERACT_TIMEOUT_MS = 15_000

_WHITESPACE_RUN = re.compile(r"\s+")


_DEFAULT_EXTRACTION_PROMPT = """\
You are a web data extractor. The user is browsing a page in an
automated session and needs you to pull structured data out of the
rendered text.

Return ONE valid JSON object that matches the requested schema. Do not
include explanations, markdown fences, or any text outside the JSON.

If a requested field cannot be confidently extracted from the page,
use ``null`` (for nullable fields) or the schema's documented default.
Never invent or guess data that the page does not contain.\
"""

_DEFAULT_LOGIN_HEURISTICS_PROMPT = """\
You are a login-form analyzer. Given a page's HTML, identify CSS
selectors for the username, password, and submit elements of the
primary login form on the page.

Return JSON of the form ``{"username_selector": "...",
"password_selector": "...", "submit_selector": "..."}``. Use ``""``
(empty string) for any selector you cannot determine confidently.\
"""


class BrowserService(Service):
    slash_namespace = "browser"
    tool_provider_name = "browser"

    # Configurable
    config_namespace = "browser"
    config_category = "Browser"

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
        self._ai_sampling: Any | None = None
        # Credential store, initialized in start().
        self._creds: CredentialStore | None = None
        # VNC session manager, initialized in start().
        self._vnc: VncSessionManager | None = None
        # Cached config values; refreshed by on_config_changed().
        self._idle_timeout = 600
        self._max_users = 8
        self._vnc_idle_timeout = 900
        self._vnc_max_per_user = 2
        self._vnc_max_total = 5
        self._extraction_prompt = _DEFAULT_EXTRACTION_PROMPT
        self._login_heuristics_prompt = _DEFAULT_LOGIN_HEURISTICS_PROMPT

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="browser",
            capabilities=frozenset({"browser", "ai_tools", "ws_handlers"}),
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

        # AI sampling capability for browser_extract. Optional — the
        # tool returns an explicit error when unavailable.
        ai_chat = resolver.get_capability("ai_chat")
        # Only require complete_one_shot duck-typed; the AI service
        # implements AISamplingProvider in interfaces/ai.py.
        if ai_chat is not None and hasattr(ai_chat, "complete_one_shot"):
            self._ai_sampling = ai_chat

        # Credential store — opens / generates the per-installation
        # Fernet key under <data_dir>/fernet.key (mode 0600).
        if self._storage is not None:
            self._creds = CredentialStore(
                storage=self._storage,
                key_path=self._data_dir / "fernet.key",
            )
            await self._creds.start()

        # VNC session manager.
        self._vnc = VncSessionManager(
            data_dir=self._data_dir / "vnc",
            idle_timeout_seconds=self._vnc_idle_timeout,
            max_per_user=self._vnc_max_per_user,
            max_total=self._vnc_max_total,
        )
        try:
            (self._data_dir / "vnc").mkdir(parents=True, exist_ok=True)
            await self._vnc.start()
        except Exception:
            logger.exception("VNC session manager failed to start")
            self._vnc = None

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
        if self._vnc is not None:
            try:
                await self._vnc.stop()
            except Exception:
                logger.exception("VNC manager stop failed")
            self._vnc = None
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
        tools: list[ToolDefinition] = list(READ_ONLY_TOOLS) + list(INTERACTION_TOOLS)
        # Only advertise the AI-assisted extract tool when an AI
        # sampling service is actually wired in.
        if self._ai_sampling is not None:
            tools.extend(SMART_TOOLS)
        return tools

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
                if name == "browser_login":
                    return await self._tool_login(user_id, arguments)
                if name == "browser_extract":
                    return await self._tool_extract(user_id, arguments)
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

    async def _tool_extract(self, user_id: str, args: dict[str, Any]) -> str:
        if self._ai_sampling is None:
            return "error: AI sampling service unavailable"
        instruction = str(args.get("instruction", "")).strip()
        if not instruction:
            return "error: instruction required"
        json_schema = str(args.get("json_schema", "")).strip()

        page = await self._get_or_create_page(user_id)
        body_text = await page.locator("body").inner_text()
        body_text = _WHITESPACE_RUN.sub(" ", body_text or "").strip()
        # Cap input — long pages blow up the model context.
        if len(body_text) > 30_000:
            body_text = body_text[:30_000] + " …[truncated]"

        user_message = (
            f"## Instruction\n{instruction}\n\n"
            f"## Schema\n{json_schema or '(none — return any sensible JSON shape)'}\n\n"
            f"## Page text\n{body_text}"
        )

        try:
            response = await self._ai_sampling.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_message)],
                system_prompt=self._extraction_prompt,
            )
        except Exception as exc:
            return f"error: AI sampling failed: {exc}"

        text = response.message.content if response and response.message else ""
        if not text:
            return "error: AI returned no content"
        # Strip ``` fences if the model added them despite the instruction.
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    async def _tool_login(self, user_id: str, args: dict[str, Any]) -> str:
        if self._creds is None:
            return "error: credential store unavailable"
        cred_id = str(args.get("credential_id", "")).strip()
        if not cred_id:
            return "error: credential_id required"
        try:
            cred: BrowserCredential = await self._creds.get(cred_id, user_id)
        except KeyError:
            return f"error: no credential with id {cred_id}"
        except PermissionError:
            return "error: that credential belongs to another user"
        page = await self._get_or_create_page(user_id)
        ok, msg = await LoginRunner(page).run(cred)
        return msg if ok else f"error: {msg}"

    # ------------------------------------------------------------------
    # WS RPC handlers (credentials)
    # ------------------------------------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "browser.credentials.list": self._ws_credentials_list,
            "browser.credentials.save": self._ws_credentials_save,
            "browser.credentials.delete": self._ws_credentials_delete,
            "browser.vnc.start": self._ws_vnc_start,
            "browser.vnc.stop": self._ws_vnc_stop,
            "browser.vnc.list": self._ws_vnc_list,
        }

    # ------------------------------------------------------------------
    # VNC accessors used by the web layer
    # ------------------------------------------------------------------

    def get_vnc_websockify_port(self, session_id: str, user_id: str) -> int | None:
        """Resolve a session_id + caller user_id to the websockify port.

        The web-layer proxy route calls this to authorize the WS upgrade
        and find the localhost TCP port to bridge to. Returns ``None``
        if the session doesn't exist or doesn't belong to ``user_id``.
        """
        if self._vnc is None:
            return None
        s = self._vnc.get_session(session_id, user_id)
        if s is None:
            return None
        self._vnc.touch(session_id)
        return s.websockify_port

    @staticmethod
    def _conn_user_id(conn: Any) -> str:
        try:
            return getattr(conn, "user_id", "") or ""
        except Exception:
            return ""

    @staticmethod
    def _err(frame: dict[str, Any], msg: str, code: int = 400) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": msg,
            "code": code,
        }

    async def _ws_credentials_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._creds is None:
            return self._err(frame, "credential store unavailable", 503)
        creds = await self._creds.list_for_user(user_id)
        return {
            "type": "browser.credentials.list.result",
            "ref": frame.get("id"),
            "credentials": [
                {
                    "id": c.id,
                    "site": c.site,
                    "label": c.label,
                    "username": c.username,
                    "login_url": c.login_url,
                }
                for c in creds
            ],
        }

    async def _ws_credentials_save(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._creds is None:
            return self._err(frame, "credential store unavailable", 503)
        cred_id = str(frame.get("credential_id", "")).strip()
        if cred_id:
            # Update — owner check via get().
            try:
                existing = await self._creds.get(cred_id, user_id)
            except KeyError:
                return self._err(frame, "credential not found", 404)
            except PermissionError:
                return self._err(frame, "not your credential", 403)
            new_password = str(frame.get("password", "")).strip()
            password = new_password if new_password else existing.password
        else:
            password = str(frame.get("password", "")).strip()
            if not password:
                return self._err(frame, "password required for new credentials")

        cred = BrowserCredential(
            id=cred_id,
            user_id=user_id,
            site=str(frame.get("site", "")).strip(),
            label=str(frame.get("label", "")).strip(),
            username=str(frame.get("username", "")).strip(),
            password=password,
            login_url=str(frame.get("login_url", "")).strip(),
            username_selector=str(frame.get("username_selector", "")).strip(),
            password_selector=str(frame.get("password_selector", "")).strip(),
            submit_selector=str(frame.get("submit_selector", "")).strip(),
        )
        if not cred.site or not cred.username:
            return self._err(frame, "site and username are required")
        saved = await self._creds.save(cred)
        return {
            "type": "browser.credentials.save.result",
            "ref": frame.get("id"),
            "id": saved.id,
            "ok": True,
        }

    async def _ws_vnc_start(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._vnc is None:
            return self._err(frame, "VNC manager unavailable", 503)
        target_url = str(frame.get("target_url", "")).strip()
        cred_id = str(frame.get("credential_id", "")).strip()
        if cred_id and not target_url and self._creds is not None:
            try:
                cred = await self._creds.get(cred_id, user_id)
                target_url = cred.login_url
            except (KeyError, PermissionError):
                pass
        try:
            session = await self._vnc.start_session(user_id, target_url=target_url)
        except RuntimeError as exc:
            return self._err(frame, str(exc), 429)
        return {
            "type": "browser.vnc.start.result",
            "ref": frame.get("id"),
            "ok": True,
            "session": {
                "id": session.session_id,
                "vnc_url": f"/api/browser/vnc/{session.session_id}/ws",
                "expires_at": "",
            },
        }

    async def _ws_vnc_stop(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._vnc is None:
            return self._err(frame, "VNC manager unavailable", 503)
        session_id = str(frame.get("session_id", "")).strip()
        if not session_id:
            return self._err(frame, "session_id required")
        owned = self._vnc.get_session(session_id, user_id)
        if owned is None:
            # Idempotent — already gone or never owned.
            return {
                "type": "browser.vnc.stop.result",
                "ref": frame.get("id"),
                "ok": True,
            }
        # Best-effort: try to ask the headed Chromium to export
        # storage_state via Playwright's CDP support before tearing
        # down. For now we rely on the user-data-dir cookies surviving
        # in-place; future work can hook this up via puppeteer/CDP to
        # extract a clean storage_state.json.
        exported = await self._vnc.stop_session(session_id)
        if exported is not None and self._pool is not None:
            try:
                await self._pool.merge_storage_state(user_id, exported)
            except Exception:
                logger.exception("merge_storage_state failed for user %s", user_id)
        return {
            "type": "browser.vnc.stop.result",
            "ref": frame.get("id"),
            "ok": True,
        }

    async def _ws_vnc_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._vnc is None:
            return {
                "type": "browser.vnc.list.result",
                "ref": frame.get("id"),
                "sessions": [],
            }
        sessions = self._vnc.list_sessions(user_id)
        return {
            "type": "browser.vnc.list.result",
            "ref": frame.get("id"),
            "sessions": [
                {
                    "id": s.session_id,
                    "vnc_url": f"/api/browser/vnc/{s.session_id}/ws",
                    "expires_at": "",
                }
                for s in sessions
            ],
        }

    async def _ws_credentials_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = self._conn_user_id(conn)
        if not user_id:
            return self._err(frame, "not authenticated", 401)
        if self._creds is None:
            return self._err(frame, "credential store unavailable", 503)
        cred_id = str(frame.get("credential_id", "")).strip()
        if not cred_id:
            return self._err(frame, "credential_id required")
        try:
            await self._creds.delete(cred_id, user_id)
        except KeyError:
            # Idempotent — already gone.
            pass
        except PermissionError:
            return self._err(frame, "not your credential", 403)
        return {
            "type": "browser.credentials.delete.result",
            "ref": frame.get("id"),
            "ok": True,
        }

    # ------------------------------------------------------------------
    # Configurable
    # ------------------------------------------------------------------

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="idle_timeout_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Close a user's browser context after this many idle "
                    "seconds. Default 600 (10 minutes)."
                ),
                default=600,
            ),
            ConfigParam(
                key="max_concurrent_users",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard cap on simultaneous browser contexts (server-wide). "
                    "Each context uses ~100-150 MB; tune to host RAM."
                ),
                default=8,
            ),
            ConfigParam(
                key="vnc_idle_timeout_seconds",
                type=ToolParameterType.INTEGER,
                description="Close inactive VNC live-login sessions after this many seconds.",
                default=900,
            ),
            ConfigParam(
                key="vnc_max_concurrent_per_user",
                type=ToolParameterType.INTEGER,
                description="Per-user cap on simultaneous VNC live-login sessions.",
                default=2,
            ),
            ConfigParam(
                key="vnc_max_concurrent_total",
                type=ToolParameterType.INTEGER,
                description="Server-wide cap on simultaneous VNC live-login sessions.",
                default=5,
            ),
            ConfigParam(
                key="extraction_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt used by browser_extract to convert "
                    "rendered page text into structured JSON."
                ),
                default=_DEFAULT_EXTRACTION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="login_heuristics_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt used to detect login-form selectors "
                    "from HTML when none are configured on the credential."
                ),
                default=_DEFAULT_LOGIN_HEURISTICS_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._idle_timeout = int(config.get("idle_timeout_seconds", 600) or 600)
        self._max_users = int(config.get("max_concurrent_users", 8) or 8)
        self._vnc_idle_timeout = int(
            config.get("vnc_idle_timeout_seconds", 900) or 900
        )
        self._vnc_max_per_user = int(
            config.get("vnc_max_concurrent_per_user", 2) or 2
        )
        self._vnc_max_total = int(
            config.get("vnc_max_concurrent_total", 5) or 5
        )
        self._extraction_prompt = (
            config.get("extraction_prompt") or _DEFAULT_EXTRACTION_PROMPT
        )
        self._login_heuristics_prompt = (
            config.get("login_heuristics_prompt") or _DEFAULT_LOGIN_HEURISTICS_PROMPT
        )
        # Live-tunable: propagate the idle timeout to the running pool
        # so a config change takes effect without a service restart.
        if self._pool is not None:
            self._pool._idle_timeout = self._idle_timeout

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
