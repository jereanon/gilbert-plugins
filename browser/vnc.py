"""VNC live-login session manager.

A VNC session is a tuple of four processes:

- ``Xvfb`` — headless X display.
- ``x11vnc`` — VNC server attached to that display.
- ``websockify`` — TCP→WebSocket bridge so a browser noVNC client can
  connect.
- ``chromium`` (headed) — running with ``DISPLAY=:N`` so its window
  appears on the Xvfb display.

After the user logs into a site through the embedded noVNC client and
clicks "Done", we export the headed Chromium's ``storage_state`` and
merge it into the user's persistent headless context (see
``ContextPool.merge_storage_state``).

Each session owns:

- A unique session_id (so RPC routes can validate ownership).
- An X display number (uniquely allocated across the host).
- A websockify port (for the in-browser noVNC client to dial).
- A temp dir for ``--user-data-dir`` so the headed Chromium has its
  own profile.

This module fakes nothing at import time — Xvfb / x11vnc / websockify
must be on PATH before ``start_session`` is called. Tests patch
``asyncio.create_subprocess_exec``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Session:
    session_id: str
    user_id: str
    display: int
    websockify_port: int
    procs: list[Any]
    user_data_dir: Path
    state_export_path: Path
    started_at: float
    last_used: float
    target_url: str = ""


def _free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VncSessionManager:
    """Per-installation manager of headed-browser VNC sessions."""

    def __init__(
        self,
        data_dir: Path,
        *,
        idle_timeout_seconds: int = 900,
        max_per_user: int = 2,
        max_total: int = 5,
        chromium_binary: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._idle_timeout = idle_timeout_seconds
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._chromium_binary = chromium_binary
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        self._next_display = 90
        self._reaper: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
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
        for sid in list(self._sessions.keys()):
            try:
                await self._stop_session_locked(sid)
            except Exception:
                logger.exception("stop_session failed for %s", sid)

    async def start_session(
        self,
        user_id: str,
        *,
        target_url: str = "",
    ) -> _Session:
        async with self._lock:
            user_count = sum(1 for s in self._sessions.values() if s.user_id == user_id)
            if user_count >= self._max_per_user:
                raise RuntimeError(
                    f"max VNC sessions per user reached ({self._max_per_user})"
                )
            if len(self._sessions) >= self._max_total:
                raise RuntimeError(
                    f"server-wide VNC session cap reached ({self._max_total})"
                )

            session_id = secrets.token_urlsafe(12)
            display = self._next_display
            self._next_display += 1
            websockify_port = _free_tcp_port()
            x11vnc_port = _free_tcp_port()

            user_data_dir = Path(
                tempfile.mkdtemp(prefix=f"browser-vnc-{user_id}-", dir=str(self._data_dir))
            )
            state_export = user_data_dir / "exported_state.json"

            procs: list[Any] = []

            # Xvfb on a unique display.
            xvfb = await asyncio.create_subprocess_exec(
                "Xvfb",
                f":{display}",
                "-screen",
                "0",
                "1280x800x24",
                "-nolisten",
                "tcp",
            )
            procs.append(xvfb)

            # Give Xvfb a moment to come up. A more robust check would
            # poll for the X socket, but a small fixed wait keeps things
            # simple for what is already a manual flow.
            await asyncio.sleep(0.5)

            # x11vnc bound to that display.
            x11vnc = await asyncio.create_subprocess_exec(
                "x11vnc",
                "-display",
                f":{display}",
                "-rfbport",
                str(x11vnc_port),
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-quiet",
            )
            procs.append(x11vnc)

            # websockify bridging the TCP VNC server to a websocket port.
            websockify = await asyncio.create_subprocess_exec(
                "websockify",
                str(websockify_port),
                f"127.0.0.1:{x11vnc_port}",
            )
            procs.append(websockify)

            # Headed Chromium pointed at Xvfb DISPLAY.
            chromium = self._chromium_binary or "chromium"
            chromium_args = [
                chromium,
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate",
                "--start-maximized",
            ]
            if target_url:
                chromium_args.append(target_url)
            chromium_proc = await asyncio.create_subprocess_exec(
                *chromium_args,
                env={"DISPLAY": f":{display}", "PATH": "/usr/bin:/bin:/usr/local/bin"},
            )
            procs.append(chromium_proc)

            session = _Session(
                session_id=session_id,
                user_id=user_id,
                display=display,
                websockify_port=websockify_port,
                procs=procs,
                user_data_dir=user_data_dir,
                state_export_path=state_export,
                started_at=time.monotonic(),
                last_used=time.monotonic(),
                target_url=target_url,
            )
            self._sessions[session_id] = session
            return session

    async def stop_session(self, session_id: str) -> Path | None:
        """Stop a session and return the path to the exported storage_state.

        Returns ``None`` if the session was not found (idempotent).
        """
        async with self._lock:
            return await self._stop_session_locked(session_id)

    async def _stop_session_locked(self, session_id: str) -> Path | None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        # Tear down processes in reverse order: chromium → websockify
        # → x11vnc → Xvfb.
        for proc in reversed(session.procs):
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in reversed(session.procs):
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, Exception):
                try:
                    proc.kill()
                except Exception:
                    pass
        # The exported_state.json is written by an out-of-band export
        # call we trigger before stop (or by the post-stop cleanup if
        # we couldn't reach the headed context). Returning the path
        # lets the caller invoke ContextPool.merge_storage_state when
        # the file exists.
        if session.state_export_path.exists():
            return session.state_export_path
        return None

    def list_sessions(self, user_id: str) -> list[_Session]:
        return [s for s in self._sessions.values() if s.user_id == user_id]

    def get_session(self, session_id: str, user_id: str) -> _Session | None:
        s = self._sessions.get(session_id)
        if s is None or s.user_id != user_id:
            return None
        return s

    def touch(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s is not None:
            s.last_used = time.monotonic()

    async def _reap_loop(self) -> None:
        interval = max(self._idle_timeout // 4, 30)
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self._reap_once()
            except Exception:
                logger.exception("VNC reap loop iteration failed")

    async def _reap_once(self) -> None:
        cutoff = time.monotonic() - self._idle_timeout
        async with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.last_used < cutoff]
            for sid in stale:
                logger.info("Reaping idle VNC session %s", sid)
                try:
                    await self._stop_session_locked(sid)
                except Exception:
                    logger.exception("failed to stop idle session %s", sid)
