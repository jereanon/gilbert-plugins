"""OpenCode backend — speaks the ``opencode serve`` HTTP API.

OpenCode (https://github.com/sst/opencode) ships an ``opencode
serve`` mode that exposes a localhost HTTP server with the
following surface we rely on:

- ``POST   /session``                       — create a new session,
  optionally pinned to a project directory.
- ``POST   /session/{id}/prompt_async``      — fire a prompt into a
  session. Returns 204 immediately; the agent works in the
  background. Used by every ``send_message`` call — Gilbert is a
  conduit, not a synchronous IDE client, so we never wait.
- ``GET    /session``                        — list sessions, most-
  recent first. Used for "what's been worked on lately" tooling
  (Phase 2 / 3).

Auth: HTTP Basic with user ``opencode`` and the password the user
sets via ``OPENCODE_SERVER_PASSWORD`` when starting ``serve``.

Connectivity: the daemon defaults to ``127.0.0.1:4096``. Since
Gilbert typically runs on a server (meridian) and the user's
coding work happens on a laptop, the practical deployment is to
start ``opencode serve --hostname <tailnet-ip>`` on the laptop and
point Gilbert at that URL via the ``server_url`` config param.
Tailscale gives us encrypted transit + a stable hostname without
exposing the agent to the public internet.

**Field-name caveat**: OpenCode is pre-1.0. The exact JSON shape
of session-create requests and list responses can drift between
versions. The reads below are defensive (try multiple known field
names, fall back to ``str(...)`` of whatever's there) so a minor
schema change at the OpenCode side won't crash the conduit. If a
specific OpenCode release breaks something here, fix the parser —
the rest of the plugin doesn't need to know.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gilbert.interfaces.coding_agent import (
    CodingAgentBackend,
    CodingAgentSendResult,
    CodingAgentSession,
)
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_DEFAULT_URL = "http://127.0.0.1:4096"


class OpenCodeBackend(CodingAgentBackend):
    """OpenCode (``opencode serve``) HTTP integration."""

    backend_name = "opencode"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="server_url",
                type=ToolParameterType.STRING,
                description=(
                    "Base URL of ``opencode serve``. The daemon's "
                    "default is http://127.0.0.1:4096 — fine when "
                    "Gilbert and OpenCode are on the same host. For "
                    "a typical setup (Gilbert on a server, OpenCode "
                    "on a laptop), run ``opencode serve --hostname "
                    "<tailnet-ip>`` and put the resulting tailnet "
                    "URL here."
                ),
                default=_DEFAULT_URL,
            ),
            ConfigParam(
                key="server_password",
                type=ToolParameterType.STRING,
                description=(
                    "Password for ``opencode serve`` (set via the "
                    "OPENCODE_SERVER_PASSWORD env var when starting "
                    "the daemon). HTTP Basic, user is ``opencode``. "
                    "Required — without it the backend reports "
                    "``available=False`` and no relays fire."
                ),
                sensitive=True,
            ),
            ConfigParam(
                key="timeout_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "HTTP request timeout. The conduit doesn't wait "
                    "on the agent's response (prompt_async returns "
                    "204), so this only caps the round-trip to the "
                    "daemon — 10s is plenty over Tailscale."
                ),
                default=10,
            ),
        ]

    def __init__(self) -> None:
        self._server_url: str = _DEFAULT_URL
        self._server_password: str = ""
        self._timeout: float = 10.0
        self._client: httpx.AsyncClient | None = None

    async def initialize(self, config: dict[str, Any]) -> None:
        self._server_url = (
            str(config.get("server_url", _DEFAULT_URL) or _DEFAULT_URL).rstrip("/")
        )
        self._server_password = str(config.get("server_password", "") or "")
        try:
            self._timeout = float(config.get("timeout_seconds", 10) or 10)
        except (TypeError, ValueError):
            self._timeout = 10.0

        # Tear down any prior client (re-init may run after a
        # Settings → Save).
        if self._client is not None:
            await self._client.aclose()
            self._client = None

        if not self._server_password:
            logger.warning(
                "OpenCode backend: no server password set — backend "
                "is offline until Settings → Code Conduit → OpenCode "
                "→ Server password is filled in"
            )
            return

        # One AsyncClient per backend lifetime — connection pool +
        # Basic auth header configured once. Reused across every
        # send / list call.
        self._client = httpx.AsyncClient(
            base_url=self._server_url,
            auth=("opencode", self._server_password),
            timeout=self._timeout,
        )
        logger.info(
            "OpenCode backend initialized (url=%s, timeout=%.1fs)",
            self._server_url,
            self._timeout,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def send_message(
        self,
        *,
        message: str,
        project_path: str,
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        if self._client is None:
            raise RuntimeError(
                "OpenCode backend not configured — set the server "
                "URL and password in Settings → Code Conduit"
            )

        # Mint a fresh session when the caller asked for one or
        # didn't supply an id. OpenCode pins each session to a
        # working directory at creation time, so we pass the
        # project path through here rather than as a per-prompt
        # field — sticks even for follow-up prompts in the same
        # session.
        if new_session or not session_id:
            session_id = await self._create_session(project_path)

        # prompt_async is fire-and-forget: 204 No Content on
        # accept, with the agent running asynchronously in the
        # daemon. Exactly what we want for the voice-loop conduit.
        resp = await self._client.post(
            f"/session/{session_id}/prompt_async",
            json={"prompt": message},
        )
        resp.raise_for_status()
        return CodingAgentSendResult(
            session_id=session_id,
            project_path=project_path,
            status="sent",
        )

    async def _create_session(self, project_path: str) -> str:
        if self._client is None:
            raise RuntimeError("OpenCode backend not configured.")
        payload: dict[str, Any] = {}
        if project_path:
            # OpenCode's session-create accepts a project directory
            # field — the exact key has varied across versions
            # (``projectPath`` / ``directory`` / ``cwd``). We send
            # all three so whichever the running daemon recognises
            # wins; unknown extras are ignored.
            payload["projectPath"] = project_path
            payload["directory"] = project_path
            payload["cwd"] = project_path
        resp = await self._client.post("/session", json=payload)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        sid = self._extract_session_id(data)
        if not sid:
            raise RuntimeError(
                f"OpenCode created a session but returned no id: {data!r}"
            )
        return sid

    @staticmethod
    def _extract_session_id(data: Any) -> str:
        """Pull the session id out of a JSON-decoded response,
        tolerating the handful of key names OpenCode has used.
        Returns ``""`` when none of the known keys are present."""
        if not isinstance(data, dict):
            return ""
        for key in ("id", "sessionId", "session_id"):
            value = data.get(key)
            if value:
                return str(value)
        # Some endpoints wrap the new object under ``data`` or
        # ``session``. Recurse into those once.
        for wrapper in ("data", "session"):
            inner = data.get(wrapper)
            if isinstance(inner, dict):
                sid = OpenCodeBackend._extract_session_id(inner)
                if sid:
                    return sid
        return ""

    async def list_sessions(
        self,
        *,
        project_path: str = "",
        limit: int = 20,
    ) -> list[CodingAgentSession]:
        if self._client is None:
            return []
        params: dict[str, Any] = {}
        if project_path:
            # Same defensive multi-key approach as session-create —
            # whichever the daemon actually filters on wins.
            params["projectPath"] = project_path

        resp = await self._client.get("/session", params=params)
        resp.raise_for_status()
        raw = resp.json() if resp.content else []
        if not isinstance(raw, list):
            # Some versions wrap the list in ``{sessions: [...]}``.
            if isinstance(raw, dict) and isinstance(raw.get("sessions"), list):
                raw = raw["sessions"]
            else:
                raw = []

        sessions: list[CodingAgentSession] = []
        for entry in raw[:limit]:
            if not isinstance(entry, dict):
                continue
            sessions.append(
                CodingAgentSession(
                    session_id=self._extract_session_id(entry),
                    project_path=str(
                        entry.get("projectPath")
                        or entry.get("directory")
                        or entry.get("cwd")
                        or ""
                    ),
                    title=str(entry.get("title") or ""),
                    last_updated=str(
                        entry.get("updatedAt")
                        or entry.get("updated_at")
                        or entry.get("lastUpdated")
                        or ""
                    ),
                )
            )
        return sessions
