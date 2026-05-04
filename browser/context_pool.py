"""Per-user Playwright BrowserContext pool with idle eviction.

One BrowserContext per user_id, keyed in an in-memory dict. Each user's
storage state (cookies + localStorage) is persisted under
``<data_dir>/users/<user_id>/state.json`` so logins survive plugin
restarts. Inactive contexts are closed after ``idle_timeout_seconds``
of no use, and their storage state is flushed on the way out.

The pool can drive either a local ``chromium.launch()`` browser or a
remote ``chromium.connect(ws_endpoint)`` browser — set ``ws_endpoint``
in the constructor when running against a Playwright-in-Docker
container. Storage state file paths are HOST-side either way: the
remote browser receives storage state as bytes over the WS protocol,
so no volume mount is needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    context: Any
    last_used: float = field(default_factory=time.monotonic)


class ContextPool:
    def __init__(
        self,
        data_dir: Path,
        playwright: Any,
        idle_timeout_seconds: int = 600,
        ws_endpoint: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._pw = playwright
        self._idle_timeout = idle_timeout_seconds
        # When set, the pool connects to a remote browser (Playwright in
        # Docker via run-server). When None, launches a local headless
        # Chromium on the host.
        self._ws_endpoint = ws_endpoint
        self._browser: Any | None = None
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._ws_endpoint is not None:
            self._browser = await self._pw.chromium.connect(self._ws_endpoint)
        else:
            self._browser = await self._pw.chromium.launch(headless=True)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        async with self._lock:
            for user_id, entry in list(self._entries.items()):
                await self._save_state_for(user_id, entry)
                try:
                    await entry.context.close()
                except Exception:
                    logger.exception("close on stop failed for user %s", user_id)
            self._entries.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.exception("browser.close failed")
            self._browser = None

    async def get_for_user(self, user_id: str) -> Any:
        if not user_id:
            raise ValueError("user_id required")
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                ctx = await self._create_context(user_id)
                entry = _Entry(context=ctx)
                self._entries[user_id] = entry
            entry.last_used = time.monotonic()
            return entry.context

    async def evict_user(self, user_id: str) -> None:
        async with self._lock:
            entry = self._entries.pop(user_id, None)
        if entry is not None:
            await self._save_state_for(user_id, entry)
            try:
                await entry.context.close()
            except Exception:
                logger.exception("close on evict failed for user %s", user_id)

    async def merge_storage_state(
        self, user_id: str, exported_state_path: Path
    ) -> None:
        """Merge a headed-browser exported storage_state into the user's
        persistent state file. Called after a VNC live-login session ends.

        The merge is permissive: cookies de-dupe by ``(name, domain, path)``
        with the headed copy winning on conflict; localStorage origins
        are unioned with the exported copy winning on key collisions.
        """
        import json

        target = self._user_state_path(user_id)
        existing: dict[str, Any] = {"cookies": [], "origins": []}
        if target.exists():
            try:
                existing = json.loads(target.read_text())
            except Exception:
                logger.exception("failed to read existing state %s", target)
        try:
            exported = json.loads(exported_state_path.read_text())
        except Exception:
            logger.exception("failed to read exported state %s", exported_state_path)
            return

        merged_cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
        for c in existing.get("cookies", []):
            merged_cookies[(c.get("name", ""), c.get("domain", ""), c.get("path", ""))] = c
        for c in exported.get("cookies", []):
            merged_cookies[(c.get("name", ""), c.get("domain", ""), c.get("path", ""))] = c

        merged_origins: dict[str, dict[str, Any]] = {}
        for o in existing.get("origins", []):
            merged_origins[o.get("origin", "")] = o
        for o in exported.get("origins", []):
            merged_origins[o.get("origin", "")] = o

        merged = {
            "cookies": list(merged_cookies.values()),
            "origins": list(merged_origins.values()),
        }
        target.write_text(json.dumps(merged))

        # Drop any in-memory context for this user so the next request
        # picks up the fresh state from disk.
        await self.evict_user(user_id)

    async def _create_context(self, user_id: str) -> Any:
        assert self._browser is not None
        state_path = self._user_state_path(user_id)
        kwargs: dict[str, Any] = {}
        if state_path.exists():
            kwargs["storage_state"] = str(state_path)
        return await self._browser.new_context(**kwargs)

    async def _reap_loop(self) -> None:
        interval = max(self._idle_timeout // 4, 15)
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._reap_once()
            except Exception:
                logger.exception("reap loop iteration failed")

    async def _reap_once(self) -> None:
        cutoff = time.monotonic() - self._idle_timeout
        async with self._lock:
            stale = [uid for uid, e in self._entries.items() if e.last_used < cutoff]
            for uid in stale:
                entry = self._entries.pop(uid)
                await self._save_state_for(uid, entry)
                try:
                    await entry.context.close()
                except Exception:
                    logger.exception("close on idle eviction failed for %s", uid)

    async def _save_state_for(self, user_id: str, entry: _Entry) -> None:
        try:
            await entry.context.storage_state(path=str(self._user_state_path(user_id)))
        except Exception:
            logger.exception("Failed to persist storage_state for user %s", user_id)

    def _user_state_path(self, user_id: str) -> Path:
        p = self._data_dir / "users" / user_id
        p.mkdir(parents=True, exist_ok=True)
        return p / "state.json"
