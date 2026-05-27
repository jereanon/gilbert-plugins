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

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.coding_agent import (
    EVENT_KIND_ATTENTION,
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    CodingAgentBackend,
    CodingAgentEvent,
    CodingAgentSendResult,
    CodingAgentSession,
)
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_DEFAULT_URL = "http://127.0.0.1:4096"

# SSE reconnect backoff. Long enough to not hammer a daemon that's
# in the middle of restarting; short enough that the user doesn't
# wait forever after a Wi-Fi blip.
_SSE_RECONNECT_DELAY_S = 2.0

# Map OpenCode's native event-type names to our severity buckets.
# Anything not listed maps to ``info`` (silent in the default
# notification policy) — that's the safer default for a daemon
# that may add new event types in future versions.
#
# The OpenCode SDK references suggest event names follow a
# ``<domain>.<verb>`` shape (e.g. ``session.idle``,
# ``message.updated``, ``permission.updated``). We match by exact
# type name where known, plus a prefix sweep for ``*.error`` and
# ``permission.*`` so a daemon revision that adds new error /
# permission events still bucket correctly.
_KNOWN_EVENT_KIND: dict[str, str] = {
    # The agent finished a turn and is back to waiting for input —
    # the headline "Claude finished" signal.
    "session.idle": EVENT_KIND_DONE,
    "session.completed": EVENT_KIND_DONE,
    "message.updated": EVENT_KIND_DONE,
    # The agent (or runtime) hit an error.
    "session.error": EVENT_KIND_ERROR,
    "message.error": EVENT_KIND_ERROR,
    # The agent wants user input — a permission grant, an
    # ambiguous-instruction clarification, etc.
    "permission.requested": EVENT_KIND_ATTENTION,
    "permission.updated": EVENT_KIND_ATTENTION,
    "session.input_requested": EVENT_KIND_ATTENTION,
}


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

    # --- Inbound event stream ───────────────────────────────────────

    async def stream_events(self) -> AsyncIterator[CodingAgentEvent]:
        """Long-lived SSE consumer for ``GET /global/event``.

        Yields one ``CodingAgentEvent`` per frame the daemon emits.
        Reconnects with a fixed delay on disconnect — OpenCode is a
        local daemon, so transient drops are usually the daemon
        restarting, not the network failing. A tighter exponential
        backoff isn't worth the complexity at this trust level.

        Returns silently (no events) when the backend isn't
        configured, so a Settings page that hasn't been filled in
        yet doesn't trip the consumer loop on startup. The service
        layer re-creates the iterator after a config reload.
        """
        if self._client is None:
            logger.info(
                "OpenCode SSE consumer not starting — backend not "
                "configured (no server password)"
            )
            return

        while True:
            try:
                # ``httpx.stream`` opens a connection that lives for
                # the duration of the ``async with``; we iterate
                # lines off it and yield events as full SSE frames
                # land. The default ``timeout`` set on the client is
                # respected on connect; we set ``timeout=None``
                # explicitly for the streaming body so a quiet
                # period (no events for minutes) doesn't trip the
                # 10-second cap.
                async with self._client.stream(
                    "GET", "/global/event", timeout=None
                ) as resp:
                    resp.raise_for_status()
                    async for event in self._parse_sse_stream(resp):
                        yield event
            except asyncio.CancelledError:
                # Service is stopping. Don't reconnect.
                raise
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "OpenCode SSE stream failed (%s); reconnecting "
                    "in %.1fs",
                    exc,
                    _SSE_RECONNECT_DELAY_S,
                )
                await asyncio.sleep(_SSE_RECONNECT_DELAY_S)
            except Exception:
                # Defensive: anything else (JSON parse, unexpected
                # response shape) is a bug in our parser, not the
                # daemon. Log loudly and back off rather than crash
                # the service.
                logger.exception(
                    "OpenCode SSE consumer raised unexpectedly; "
                    "reconnecting in %.1fs",
                    _SSE_RECONNECT_DELAY_S,
                )
                await asyncio.sleep(_SSE_RECONNECT_DELAY_S)

    async def _parse_sse_stream(
        self,
        resp: httpx.Response,
    ) -> AsyncIterator[CodingAgentEvent]:
        """Parse a Server-Sent Events stream into ``CodingAgentEvent``
        instances. One yield per complete frame (blank-line
        delimited).

        Hand-rolled rather than pulling in ``httpx-sse`` for the
        sake of a stdlib-only plugin. SSE is small: lines of the
        form ``event: <type>`` and ``data: <payload>``, with a
        blank line separating frames. We accumulate the current
        frame's ``event`` + ``data`` lines and emit on blank.
        Multi-line ``data:`` is joined with ``\\n`` per spec.
        """
        event_type = ""
        data_lines: list[str] = []
        async for raw_line in resp.aiter_lines():
            # httpx strips the trailing newline but preserves the
            # line content. Treat ``""`` as the frame separator.
            if raw_line == "":
                if event_type or data_lines:
                    yield self._sse_frame_to_event(
                        event_type, "\n".join(data_lines)
                    )
                event_type = ""
                data_lines = []
                continue
            # Comments (lines starting with ``:``) — used by some
            # servers as keep-alive pings. Ignore.
            if raw_line.startswith(":"):
                continue
            if raw_line.startswith("event:"):
                event_type = raw_line[len("event:") :].strip()
            elif raw_line.startswith("data:"):
                data_lines.append(raw_line[len("data:") :].lstrip())
            # Unknown field name (``id:``, ``retry:``, …) — SSE
            # spec says ignore.

    @staticmethod
    def _sse_frame_to_event(
        event_type: str,
        data: str,
    ) -> CodingAgentEvent:
        """Build a ``CodingAgentEvent`` from a parsed SSE frame.

        Robust to:
        - ``event_type`` empty (some senders only set ``data:``
          with the type inlined inside the JSON).
        - ``data`` not being JSON (rare — older OpenCode versions
          emitted plain strings for some frames).
        """
        payload: dict[str, Any] = {}
        if data:
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                # Plain-string ``data:`` — stash it as the detail
                # so the SPA feed still has something to show.
                payload = {"detail": data}

        # If the SSE ``event:`` line wasn't set, fall back to a
        # ``type`` field inside the payload — OpenCode's stream
        # has mixed both shapes across versions.
        resolved_type = event_type or str(payload.get("type") or "")
        kind = OpenCodeBackend._kind_for_event_type(resolved_type)

        return CodingAgentEvent(
            session_id=str(
                payload.get("sessionId")
                or payload.get("session_id")
                or payload.get("id")
                or ""
            ),
            project_path=str(
                payload.get("projectPath")
                or payload.get("directory")
                or payload.get("cwd")
                or ""
            ),
            kind=kind,
            summary=OpenCodeBackend._summary_for_payload(
                resolved_type, kind, payload
            ),
            detail=str(payload.get("detail") or payload.get("message") or ""),
            timestamp=str(
                payload.get("timestamp")
                or payload.get("updatedAt")
                or payload.get("createdAt")
                or ""
            ),
            raw_type=resolved_type,
        )

    @staticmethod
    def _kind_for_event_type(event_type: str) -> str:
        """Map an OpenCode event-type string to a severity bucket.
        Exact match wins; prefix sweeps catch new variants of
        known categories (``foo.error``, ``permission.foo``)."""
        if not event_type:
            return EVENT_KIND_INFO
        if event_type in _KNOWN_EVENT_KIND:
            return _KNOWN_EVENT_KIND[event_type]
        # Prefix fallback. ``.error`` suffix on any domain is an
        # error; ``permission.*`` is always attention-grade.
        if event_type.endswith(".error") or "error" in event_type.lower():
            return EVENT_KIND_ERROR
        if event_type.startswith("permission."):
            return EVENT_KIND_ATTENTION
        return EVENT_KIND_INFO

    @staticmethod
    def _summary_for_payload(
        event_type: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str:
        """Build a short, TTS-friendly one-liner from the payload.

        OpenCode payloads vary by type — we look at a handful of
        common fields (``title``, ``message``, ``error``) and fall
        back to the event type when nothing readable is available.
        """
        for field in ("title", "summary", "message", "error", "text"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        # Default voice-friendly labels per severity. Keeps the
        # spoken reply useful when the payload only carries
        # structural metadata.
        if kind == EVENT_KIND_DONE:
            return "Coding agent finished."
        if kind == EVENT_KIND_ERROR:
            return "Coding agent errored."
        if kind == EVENT_KIND_ATTENTION:
            return "Coding agent is waiting on you."
        return event_type or "Coding-agent event."
