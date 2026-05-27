"""CodeConduitService — relay between Gilbert and a coding agent.

Gilbert is a *conduit*, not a coder. This service owns:

- Resolving the operator-configured project alias map ("gilbert"
  → "/Users/jeremy/projects/gilbert") so spoken commands stay
  natural.
- Picking the active ``CodingAgentBackend`` (currently just
  OpenCode) and forwarding sends to it.
- Exposing the AI tool ``code_send`` so the LLM can fire a relay
  mid-conversation when the user says "tell Claude to ...".
- Exposing a slash command ``/code send`` so the same flow works
  from the chat UI.
- Exposing a "Test connection" action on the Settings page so the
  operator can verify the OpenCode URL + password before relying
  on it during a voice session.

Out of scope for Phase 1:

- Inbound channel (agent → Gilbert notifications). Lands in
  Phase 2 via an SSE consumer + event-bus publish.
- Per-Gilbert-user agent configs. Single-tenant for now; the
  ``CodingConduitProvider`` capability surface is shaped so adding
  per-user routing later is an additive change.
- Live SPA page. Phase 3 polish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any

from gilbert.interfaces.coding_agent import (
    EVENT_KIND_ATTENTION,
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    CodingAgentBackend,
    CodingAgentEvent,
    CodingAgentSendResult,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


_DEFAULT_BACKEND = "opencode"

# Cap on the in-memory ring buffer of recent events. The buffer
# backs the ``code_recent_activity`` AI tool and the future
# ``/coding`` SPA feed. 200 events is ~hours of background activity
# for a single-user setup — enough to answer "what did the agent
# do today?" without unbounded memory growth.
_RECENT_EVENT_CAP = 200

# Bus event types the conduit publishes. Anyone (voice-brain,
# Mentra, push notifications, the SPA) can subscribe to surface
# notifications however they want.
BUS_EVENT_NOTIFICATION = "code.notification"


class CodeConduitService(Service):
    """Conduit between Gilbert and a coding agent.

    Capabilities: ``code_conduit``, ``ai_tools``.
    """

    # Short, voice-friendly slash namespace. Per
    # std-plugins/CLAUDE.md, plugin services that expose tools must
    # set this rather than relying on the directory-name fallback
    # ("code-conduit." would be ugly).
    slash_namespace = "code"

    def __init__(self) -> None:
        self._enabled: bool = False
        self._backend_name: str = _DEFAULT_BACKEND
        self._backend: CodingAgentBackend | None = None
        self._settings: dict[str, Any] = {}
        # Operator-configured project alias map. Keys are short
        # human names ("gilbert"); values are absolute paths
        # ("/Users/jeremy/projects/gilbert"). Populated from the
        # ``project_aliases`` multiline config field.
        self._project_aliases: dict[str, str] = {}
        self._default_project_alias: str = ""
        # Inbound notification plumbing — populated in ``start()``.
        self._bus: EventBus | None = None
        self._event_pump_task: asyncio.Task[None] | None = None
        # Bounded ring buffer of recent events backing the
        # ``code_recent_activity`` AI tool. Newer events at the
        # right; ``maxlen`` evicts the oldest automatically.
        self._recent_events: deque[CodingAgentEvent] = deque(
            maxlen=_RECENT_EVENT_CAP
        )

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="code_conduit",
            capabilities=frozenset({"code_conduit", "ai_tools"}),
            optional=frozenset({"configuration", "event_bus"}),
            toggleable=True,
            toggle_description=(
                "Relay messages between Gilbert and a coding agent "
                "(OpenCode / Claude Code)"
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Code Conduit service disabled")
            return

        # Grab the bus before applying config — the event pump
        # spawned by ``_apply_config`` wants to know whether it has
        # somewhere to publish. Missing event bus is non-fatal:
        # events still land in the local ring buffer for the
        # ``code_recent_activity`` tool.
        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._bus = bus_svc.bus

        self._enabled = True
        await self._apply_config(section)

    async def stop(self) -> None:
        # Cancel the inbound event pump before closing the backend
        # — otherwise the pump's in-flight HTTP request races with
        # the backend's ``aclose()`` and surfaces as scary
        # "Cannot send a request, as the client has been closed"
        # warnings in the log.
        if self._event_pump_task is not None and not self._event_pump_task.done():
            self._event_pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._event_pump_task
        self._event_pump_task = None

        if self._backend is not None:
            await self._backend.close()
            self._backend = None

    # --- Configurable ───────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "code_conduit"

    @property
    def config_category(self) -> str:
        # "Integrations" rather than "Intelligence" — this is
        # routing, not model selection. Sits next to Slack /
        # Discord in the Settings tab.
        return "Integrations"

    def config_params(self) -> list[ConfigParam]:
        registered = CodingAgentBackend.registered_backends()
        params: list[ConfigParam] = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description=(
                    "Which coding-agent integration to relay to. "
                    "Currently only 'opencode' ships; 'claude_code' "
                    "is planned for Phase 3."
                ),
                default=_DEFAULT_BACKEND,
                restart_required=True,
                choices=tuple(sorted(registered.keys())) or (_DEFAULT_BACKEND,),
            ),
            ConfigParam(
                key="default_project_alias",
                type=ToolParameterType.STRING,
                description=(
                    "Alias to use when the user doesn't name a "
                    "project ('tell Claude to write tests' with no "
                    "project mentioned). Must match a key in "
                    "``project_aliases`` — empty means require the "
                    "user to name one explicitly."
                ),
                default="",
            ),
            ConfigParam(
                key="project_aliases",
                type=ToolParameterType.STRING,
                description=(
                    "Map of short aliases to absolute project "
                    "paths, one per line. Format: ``alias=/abs/path``. "
                    "Lets the user say 'the gilbert project' instead "
                    "of pasting a full path. Lines starting with # "
                    "and blank lines are ignored."
                ),
                default="",
                multiline=True,
            ),
        ]

        # Forward the active backend's params under ``settings.<key>``.
        backend_cls = registered.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """All connection-y params are restart_required, so a live
        reload only needs to refresh the alias map + default. The
        backend itself reloads on service restart.
        """
        self._default_project_alias = str(
            config.get("default_project_alias", "") or ""
        )
        self._project_aliases = self._parse_aliases(
            str(config.get("project_aliases", "") or "")
        )

    # --- ConfigActionProvider ───────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify the configured coding agent is reachable "
                    "and the password works. Doesn't send a real "
                    "prompt — just lists sessions, which is the "
                    "lightest read available."
                ),
            ),
        ]

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key != "test_connection":
            return ConfigActionResult(
                status="error",
                message=f"Unknown action: {key}",
            )
        return await self._action_test_connection()

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._backend is None or not self._backend.available:
            return ConfigActionResult(
                status="error",
                message=(
                    "Backend not configured — fill in the server URL "
                    "and password, save, then try again."
                ),
            )
        try:
            sessions = await self._backend.list_sessions(limit=1)
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Couldn't reach the coding agent: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to {self._backend_name} "
                f"({len(sessions)} session(s) visible)."
            ),
        )

    # --- ToolProvider ───────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "code_conduit"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        if not self._enabled or self._backend is None:
            return []
        return [
            ToolDefinition(
                name="code_send",
                slash_group="code",
                slash_command="send",
                slash_help=(
                    "Relay a message to your coding agent without "
                    "waiting for a response."
                ),
                description=(
                    "Relay a message from the user to their coding "
                    "agent (OpenCode / Claude Code) running on the "
                    "user's own machine. Use this whenever the user "
                    "wants you to PASS ALONG an instruction to their "
                    "coder — phrases like 'tell Claude to ...', "
                    "'ask OpenCode to ...', 'have the coding agent "
                    "...', 'send this to my code session ...'. "
                    "\n\n"
                    "Gilbert is a CONDUIT, not a coder. Never edit "
                    "or paraphrase the user's text — pass it through "
                    "verbatim so their coding agent gets the exact "
                    "instruction they intended. The send is fire-"
                    "and-forget: it returns 'sent' as soon as the "
                    "agent receives the message, NOT when the agent "
                    "finishes. The agent's response will surface "
                    "asynchronously via a notification later. "
                    "\n\n"
                    "When the user names a project ('the gilbert "
                    "branch', 'the mentra project'), pass that as "
                    "``project``. The service maps aliases to "
                    "absolute paths."
                ),
                parameters=[
                    ToolParameter(
                        name="message",
                        type=ToolParameterType.STRING,
                        description=(
                            "The user's instruction, verbatim. Do "
                            "NOT rephrase or summarize."
                        ),
                        required=True,
                    ),
                    ToolParameter(
                        name="project",
                        type=ToolParameterType.STRING,
                        description=(
                            "Project alias (e.g. 'gilbert') or "
                            "absolute path. Falls back to the "
                            "operator-configured default when omitted."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="new_session",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "Force a fresh session even if one is "
                            "active. Use when the user says 'forget "
                            "what we were doing' or starts a "
                            "clearly unrelated request."
                        ),
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="code_recent_activity",
                slash_group="code",
                slash_command="recent",
                slash_help=(
                    "Summarize what the coding agent has been doing "
                    "recently — finishes, errors, prompts for input."
                ),
                description=(
                    "Report what the user's coding agent (OpenCode "
                    "/ Claude Code) has done recently — completed "
                    "tasks, errors, and pending attention requests. "
                    "USE THIS when the user asks 'what has the "
                    "coding agent done?', 'is Claude still working "
                    "on that?', 'any progress on the gilbert "
                    "branch?', or any variation. Returns a short "
                    "voice-friendly summary; defaults to the last "
                    "10 notable events but accepts ``limit`` for "
                    "longer / shorter windows and ``kind`` to "
                    "filter to one severity bucket."
                ),
                parameters=[
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description=(
                            "How many recent events to surface. "
                            "Default 10; cap silently enforced at "
                            "the buffer size."
                        ),
                        required=False,
                        default=10,
                    ),
                    ToolParameter(
                        name="kind",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional severity filter: 'done' "
                            "(finished tasks), 'error', 'attention' "
                            "(waiting on user), or 'info' (tool "
                            "calls / progress). Omit for all "
                            "notable events (done / error / "
                            "attention; ``info`` is filtered out "
                            "by default to keep the summary "
                            "TTS-friendly)."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "code_send":
            return await self._tool_code_send(arguments)
        if name == "code_recent_activity":
            return await self._tool_code_recent_activity(arguments)
        raise KeyError(f"code_conduit has no tool {name!r}")

    async def _tool_code_send(self, arguments: dict[str, Any]) -> str:
        message = str(arguments.get("message", "") or "").strip()
        if not message:
            return (
                "I need an actual message to relay — what should I "
                "tell the coding agent?"
            )
        project_input = str(arguments.get("project", "") or "").strip()
        new_session = bool(arguments.get("new_session", False))

        try:
            result = await self.send_message(
                message=message,
                project=project_input,
                new_session=new_session,
            )
        except RuntimeError as exc:
            return f"I couldn't reach the coding agent: {exc}"
        except Exception as exc:
            logger.exception("code_send failed unexpectedly")
            return (
                f"I tried to relay that, but the coding agent "
                f"errored: {exc}"
            )

        # Voice-friendly confirmation. Backend tells us where it
        # actually went — we surface that so the user gets the
        # right project name in the spoken reply.
        if result.project_path:
            project_label = self._friendly_label_for(result.project_path)
            return (
                f"Sent to {self._backend_name} on {project_label}."
            )
        return f"Sent to {self._backend_name}."

    # --- Public capability — CodingConduitProvider ─────────────────

    async def send_message(
        self,
        *,
        message: str,
        project: str = "",
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        """Resolve ``project`` (alias or path), then forward to the
        active backend. Empty ``project`` falls back to the
        operator-configured default alias.

        Implements ``CodingConduitProvider``.
        """
        if self._backend is None:
            raise RuntimeError(
                "Code Conduit service is not enabled — turn it on in "
                "Settings → Services and configure the agent URL + "
                "password under Settings → Integrations → Code Conduit."
            )

        project_path = self._resolve_project(project)
        return await self._backend.send_message(
            message=message,
            project_path=project_path,
            session_id=session_id,
            new_session=new_session,
        )

    def recent_events(
        self,
        *,
        limit: int = 50,
        kind: str = "",
    ) -> list[CodingAgentEvent]:
        """Return the most recent events from the in-memory ring
        buffer, newest first. Optional ``kind`` filter buckets to
        one severity; empty means "all kinds".

        Exposed for the SPA / WS RPC + the ``code_recent_activity``
        AI tool. The buffer is bounded (see ``_RECENT_EVENT_CAP``)
        so callers can't ask for arbitrary history — they get a
        recent slice, not a query interface.
        """
        events = list(self._recent_events)
        events.reverse()  # newest first
        if kind:
            events = [e for e in events if e.kind == kind]
        if limit > 0:
            events = events[:limit]
        return events

    # --- Inbound event pump ─────────────────────────────────────────

    async def _run_event_pump(self) -> None:
        """Background task that consumes ``backend.stream_events()``
        and fans each event out to:

        - the in-memory ring buffer (for the ``code_recent_activity``
          tool and the future SPA feed)
        - the Gilbert event bus as ``code.notification``, when a
          bus is wired up — voice-brain / Mentra / push-notification
          / SPA-feed consumers subscribe to this.

        Backends that have no inbound channel return immediately
        from ``stream_events``; the pump task then exits cleanly,
        not as an error. Backends that DO have a channel are
        expected to reconnect internally — if the iterator
        terminates without an error, that's the "we're done" signal
        and the pump exits too.
        """
        if self._backend is None:
            return
        try:
            async for event in self._backend.stream_events():
                self._recent_events.append(event)
                if self._bus is not None:
                    await self._publish_event(event)
                # Log notable events at INFO so journalctl shows
                # the live signal; ``info`` (default) events stay
                # at DEBUG so a chatty agent doesn't flood the log.
                if event.kind in (
                    EVENT_KIND_DONE,
                    EVENT_KIND_ERROR,
                    EVENT_KIND_ATTENTION,
                ):
                    logger.info(
                        "Code Conduit event: kind=%s session=%s "
                        "raw_type=%s — %s",
                        event.kind,
                        event.session_id or "?",
                        event.raw_type or "?",
                        event.summary[:120],
                    )
                else:
                    logger.debug(
                        "Code Conduit event (info): raw_type=%s "
                        "session=%s",
                        event.raw_type or "?",
                        event.session_id or "?",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The backend's stream_events should swallow its own
            # transient errors and keep reconnecting. If we still
            # get here, the iterator gave up — log loudly so the
            # operator notices the inbound channel is dead.
            logger.exception(
                "Code Conduit inbound event pump exited "
                "unexpectedly — inbound notifications are OFF "
                "until the service restarts"
            )

    async def _publish_event(self, event: CodingAgentEvent) -> None:
        """Translate a ``CodingAgentEvent`` into a bus ``Event`` and
        publish. The data dict carries every interesting field so
        subscribers can route + format without round-tripping
        through any private API."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(
                Event(
                    event_type=BUS_EVENT_NOTIFICATION,
                    source="code_conduit",
                    data={
                        "kind": event.kind,
                        "summary": event.summary,
                        "detail": event.detail,
                        "session_id": event.session_id,
                        "project_path": event.project_path,
                        "timestamp": event.timestamp,
                        "raw_type": event.raw_type,
                        "backend": self._backend_name,
                    },
                )
            )
        except Exception:
            logger.exception(
                "Code Conduit: failed to publish bus notification — "
                "ring buffer still has the event"
            )

    # --- Recent-activity tool ────────────────────────────────────────

    async def _tool_code_recent_activity(
        self,
        arguments: dict[str, Any],
    ) -> str:
        """TTS-friendly summary of recent coding-agent events. The
        default filter excludes ``info`` (tool calls / progress)
        because those events flood the buffer faster than is
        useful to read aloud; pass ``kind="info"`` explicitly when
        debugging."""
        limit_raw = arguments.get("limit", 10)
        try:
            limit = max(1, int(limit_raw))
        except (TypeError, ValueError):
            limit = 10
        kind_filter = str(arguments.get("kind", "") or "").strip().lower()

        events = list(self._recent_events)
        events.reverse()  # newest first
        if kind_filter:
            events = [e for e in events if e.kind == kind_filter]
        else:
            # Default view: hide ``info``-grade noise. Users asking
            # "what's been going on?" want the notable beats, not
            # every tool call.
            events = [e for e in events if e.kind != EVENT_KIND_INFO]
        events = events[:limit]

        if not events:
            if kind_filter:
                return (
                    f"No recent {kind_filter} events from the "
                    "coding agent."
                )
            return (
                "Nothing notable from the coding agent recently — "
                "either it's idle, or only info-grade events have "
                "fired. Ask again with kind='info' to see those."
            )

        # Count by kind for the headline line.
        counts: dict[str, int] = {}
        for e in events:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        headline_parts = []
        for k in (
            EVENT_KIND_DONE,
            EVENT_KIND_ATTENTION,
            EVENT_KIND_ERROR,
            EVENT_KIND_INFO,
        ):
            if counts.get(k):
                headline_parts.append(f"{counts[k]} {k}")
        headline = f"Recent coding-agent activity ({', '.join(headline_parts)}):"

        lines = [headline]
        for e in events:
            project = self._friendly_label_for(e.project_path) if e.project_path else ""
            project_tag = f" on {project}" if project else ""
            lines.append(f"- {e.kind.upper()}{project_tag}: {e.summary}")
        return "\n".join(lines)

    # --- Internals ──────────────────────────────────────────────────

    async def _apply_config(self, section: dict[str, Any]) -> None:
        self._backend_name = (
            str(section.get("backend", _DEFAULT_BACKEND) or _DEFAULT_BACKEND)
        )
        self._settings = dict(section.get("settings") or {})
        self._default_project_alias = str(
            section.get("default_project_alias", "") or ""
        )
        self._project_aliases = self._parse_aliases(
            str(section.get("project_aliases", "") or "")
        )

        registered = CodingAgentBackend.registered_backends()
        backend_cls = registered.get(self._backend_name)
        if backend_cls is None:
            logger.error(
                "Unknown coding-agent backend %r — known: %s",
                self._backend_name,
                sorted(registered.keys()),
            )
            return

        self._backend = backend_cls()
        await self._backend.initialize(self._settings)
        if self._backend.available:
            logger.info(
                "Code Conduit started (backend=%s, %d project alias(es))",
                self._backend_name,
                len(self._project_aliases),
            )
            # Kick off the inbound-event pump. Wrapped in a Task so
            # the SSE consumer's reconnect loop runs concurrently
            # with the rest of the service — and so ``stop()`` can
            # cancel it cleanly.
            self._event_pump_task = asyncio.create_task(
                self._run_event_pump(),
                name="code_conduit.event_pump",
            )
        else:
            logger.warning(
                "Code Conduit backend %s loaded but not ready — "
                "check settings.server_url / settings.server_password",
                self._backend_name,
            )

    def _resolve_project(self, project_input: str) -> str:
        """Map a user-supplied project (alias or absolute path) to
        an absolute path. Empty input falls back to the default
        alias. Unknown aliases fall back to passing the input
        through as-is — backends can decide whether to accept a
        relative-looking value or reject it.
        """
        candidate = project_input.strip()
        if not candidate:
            candidate = self._default_project_alias.strip()
        if not candidate:
            return ""
        # Absolute path → use directly.
        if candidate.startswith("/"):
            return candidate
        # Alias hit → resolve.
        resolved = self._project_aliases.get(candidate)
        if resolved:
            return resolved
        # Unknown — pass through. Backend may still recognise it
        # (e.g. project name registered in OpenCode's own list).
        return candidate

    def _friendly_label_for(self, abs_path: str) -> str:
        """Reverse-lookup an alias for a path so the spoken reply
        says 'the gilbert project' instead of '/Users/jeremy/...'.
        Falls back to the basename of the path."""
        for alias, path in self._project_aliases.items():
            if path == abs_path:
                return f"the {alias} project"
        # No alias match — use the last path segment, which reads
        # cleaner than the full path over TTS.
        tail = abs_path.rstrip("/").rsplit("/", 1)[-1]
        return tail or abs_path

    @staticmethod
    def _parse_aliases(raw: str) -> dict[str, str]:
        """Parse the ``alias=/abs/path`` multiline format. Tolerant
        of blank lines, comments (#), and stray whitespace."""
        result: dict[str, str] = {}
        for line in (raw or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                logger.warning(
                    "Code Conduit: ignoring malformed alias line "
                    "(expected 'alias=/abs/path'): %r",
                    stripped,
                )
                continue
            alias, _, path = stripped.partition("=")
            alias = alias.strip()
            path = path.strip()
            if not alias or not path:
                continue
            result[alias] = path
        return result
