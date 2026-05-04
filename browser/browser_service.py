"""BrowserService — owns the Playwright instance, the per-user context
pool, the credential store, and (later) the VNC session manager.

Implements ``Service`` plus, as later phases land, ``ToolProvider``,
``WsHandlerProvider``, and ``Configurable``. The plugin's ``setup()``
registers a single instance with the ServiceManager.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

from .context_pool import ContextPool

logger = logging.getLogger(__name__)


class BrowserService(Service):
    slash_namespace = "browser"

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
        # Resolved capabilities, set in start().
        self._workspace: Any | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="browser",
            capabilities=frozenset({"browser"}),
            requires=frozenset(),
            optional=frozenset({"workspace", "configuration", "ai_chat"}),
            ai_calls=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._workspace = resolver.get_capability("workspace")
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
