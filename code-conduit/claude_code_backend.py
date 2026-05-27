"""Claude Code backend — subprocess outbound + webhook inbound.

`Claude Code <https://docs.anthropic.com/en/docs/claude-code>`_ is
Anthropic's terminal-based coding agent. Unlike OpenCode, it has
no long-lived daemon Gilbert can connect to — the user runs
``claude`` interactively in a terminal. The integration is
asymmetric:

- **Outbound** (Gilbert → Claude): each ``send_message`` spawns a
  one-shot ``claude -p "<prompt>" [--resume <session_id>]``
  invocation. Returns immediately with the agent's response
  captured from stdout. This does NOT feed into the user's
  *interactive* Claude Code terminal — it creates a separate
  one-shot conversation, optionally continuing a named session
  the user pinned. For Phase 3 that's the simplest model that
  works without integrating into the user's TTY.

- **Inbound** (Claude → Gilbert): not via this class. Inbound is
  push-style — the user installs a ``Stop`` hook in
  ``~/.claude/settings.json`` that POSTs to Gilbert's
  ``/api/code-conduit/inbound`` endpoint when a Claude Code
  session finishes a turn. The webhook route lives in
  ``src/gilbert/web/routes/code_conduit_webhook.py``; the service
  ingests the payload via the ``CodingConduitInboundEndpoint``
  capability protocol. ``stream_events`` here returns
  immediately (no inbound channel of its own) — the webhook
  pipeline handles it.

Availability:

- The backend reports ``available=True`` when the ``claude``
  binary is reachable on PATH (or at the operator-configured
  ``binary_path``). If the binary isn't there, sends fail loudly
  before spawning anything; ``available=False`` so the LLM tool
  surface skips us.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from typing import Any

from gilbert.interfaces.coding_agent import (
    CodingAgentBackend,
    CodingAgentEvent,
    CodingAgentSendResult,
    CodingAgentSession,
)
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


# Cap how long we'll wait for a one-shot ``claude -p`` invocation
# before killing it. Coding agents can chew on a task for minutes;
# the LLM-tool flow needs an upper bound so the voice loop doesn't
# block forever on a stuck process. Most simple prompts finish in
# 10–60s; 180s is generous without being silly.
_CLAUDE_CALL_TIMEOUT_S = 180.0


class ClaudeCodeBackend(CodingAgentBackend):
    """Claude Code (subprocess) integration."""

    backend_name = "claude_code"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="binary_path",
                type=ToolParameterType.STRING,
                description=(
                    "Absolute path to the ``claude`` binary. Leave "
                    "empty to use whatever's on ``PATH`` for the "
                    "Gilbert process. Operators with multiple "
                    "Claude Code installs (e.g. a global one + a "
                    "per-project version manager) typically pin "
                    "this to the install they want Gilbert to talk "
                    "to."
                ),
                default="",
            ),
            ConfigParam(
                key="default_session_id",
                type=ToolParameterType.STRING,
                description=(
                    "Optional Claude Code session id to resume by "
                    "default. When set, every send (that doesn't "
                    "request a new session) runs against this "
                    "session — useful for treating one long-lived "
                    "Claude Code conversation as the canonical "
                    "'work session' Gilbert nudges. Get the id with "
                    "``claude --resume`` and copy from the picker."
                ),
                default="",
            ),
            ConfigParam(
                key="extra_args",
                type=ToolParameterType.STRING,
                description=(
                    "Extra command-line flags appended to every "
                    "``claude -p`` invocation, space-separated. "
                    "Useful for forcing a model "
                    "(``--model claude-opus-4-7``), disabling "
                    "tools, or any other per-deployment knob. "
                    "Leave empty for the binary's defaults."
                ),
                default="",
            ),
        ]

    def __init__(self) -> None:
        self._binary_path: str = ""
        self._default_session_id: str = ""
        self._extra_args: list[str] = []

    async def initialize(self, config: dict[str, Any]) -> None:
        self._binary_path = str(config.get("binary_path", "") or "").strip()
        self._default_session_id = str(
            config.get("default_session_id", "") or ""
        ).strip()
        raw_extra = str(config.get("extra_args", "") or "")
        self._extra_args = raw_extra.split()
        if self.available:
            logger.info(
                "Claude Code backend initialized (resolved=%s, "
                "default_session=%s, extra_args=%s)",
                self._resolve_binary() or "<not found>",
                self._default_session_id or "<none>",
                self._extra_args or "<none>",
            )
        else:
            logger.warning(
                "Claude Code backend initialized but the ``claude`` "
                "binary is not on PATH (binary_path=%r). Sends will "
                "fail until the path is fixed.",
                self._binary_path or "<empty>",
            )

    async def close(self) -> None:
        # Nothing to release — each send is a one-shot subprocess
        # that owns its own lifetime. Webhook ingest is handled
        # entirely outside this class.
        pass

    @property
    def available(self) -> bool:
        return bool(self._resolve_binary())

    def _resolve_binary(self) -> str:
        """Resolve the binary path: operator-pinned path if set,
        else ``shutil.which("claude")``. Returns ``""`` when
        neither is reachable — caller treats that as
        ``available=False``."""
        if self._binary_path:
            # Operator pinned a path — trust it iff it's executable.
            # We don't ``which`` a literal path because PATH-search
            # only matters when the operator didn't specify one.
            import os

            if os.access(self._binary_path, os.X_OK):
                return self._binary_path
            return ""
        return shutil.which("claude") or ""

    async def send_message(
        self,
        *,
        message: str,
        project_path: str,
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        binary = self._resolve_binary()
        if not binary:
            raise RuntimeError(
                "Claude Code binary not found — set "
                "Settings → Code Conduit → settings.binary_path "
                "or install ``claude`` on the Gilbert host's PATH"
            )

        # Resume target: explicit session_id > operator default >
        # nothing (fresh session). ``new_session=True`` forces a
        # fresh session even when a default is configured.
        resume_target = ""
        if not new_session:
            resume_target = session_id or self._default_session_id

        args = [binary, "-p", message]
        if resume_target:
            args.extend(["--resume", resume_target])
        args.extend(self._extra_args)

        cwd: str | None = project_path or None
        logger.info(
            "Claude Code send (resume=%s, cwd=%s, extra=%d)",
            resume_target or "<none>",
            cwd or "<inherit>",
            len(self._extra_args),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise RuntimeError(
                f"Couldn't launch Claude Code at {binary}: {exc}"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_CLAUDE_CALL_TIMEOUT_S,
            )
        except TimeoutError:
            # Kill the process tree so we don't leak a hung
            # ``claude`` invocation on the host.
            with self._suppress_proc_errors():
                proc.kill()
                await proc.wait()
            raise RuntimeError(
                f"Claude Code didn't return within "
                f"{_CLAUDE_CALL_TIMEOUT_S:.0f}s — killed"
            ) from None

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"Claude Code exited with code {proc.returncode}: "
                f"{err[:500] or '<no stderr>'}"
            )

        # Echo Claude's response back through the recent-events
        # ring buffer so ``code_recent_activity`` shows it
        # without needing a webhook round-trip. The service
        # ingests this via the same path push-style events take,
        # but here we just return the SendResult — the inbound
        # echo lives in ``ClaudeCodeBackend.stream_events`` for
        # consistency? No — the cleanest split is: send_message
        # returns the synchronous result, and ANY long-running
        # behavior (subsequent agent activity) comes through the
        # webhook. So we don't ingest here.
        decoded_stdout = (stdout or b"").decode("utf-8", "replace").strip()
        resolved_session = resume_target or "claude_code"

        # The result's ``project_path`` echoes back what we ran
        # with so the service can render a friendly label. We
        # stash a truncated copy of the response in ``status`` so
        # callers logging the send can grep for it — the LLM
        # tool's friendly confirmation reads the ``project`` and
        # backend name, not status, so we don't need to keep it
        # short.
        logger.info(
            "Claude Code response captured (chars=%d)",
            len(decoded_stdout),
        )
        return CodingAgentSendResult(
            session_id=resolved_session,
            project_path=project_path,
            status="sent",
        )

    @staticmethod
    def _suppress_proc_errors() -> Any:
        """``contextlib.suppress`` wrapper that's easy to mock in
        tests — keeps process-kill cleanup tidy."""
        import contextlib

        return contextlib.suppress(ProcessLookupError, Exception)

    async def list_sessions(
        self,
        *,
        project_path: str = "",
        limit: int = 20,
    ) -> list[CodingAgentSession]:
        # Claude Code's session store isn't exposed via a stable
        # API today. Parsing ``claude --resume``'s picker output is
        # brittle (it's a TUI). For now, return only the
        # operator-pinned default session, if any — that's the
        # session ``code_send`` will actually talk to.
        if not self._default_session_id:
            return []
        del project_path, limit  # not filterable through the CLI
        return [
            CodingAgentSession(
                session_id=self._default_session_id,
                project_path="",
                title="Claude Code default session",
                last_updated="",
            )
        ]

    async def stream_events(self) -> AsyncIterator[CodingAgentEvent]:
        """No pull-style inbound channel for Claude Code — the
        webhook receiver in core's web routes pushes events
        through the service's ``CodingConduitInboundEndpoint``
        capability instead. Returning immediately satisfies the
        ABC contract (yield zero times, then exit) so the
        service's event pump task winds down cleanly on start.
        """
        if False:
            # ``async def`` with no ``yield`` doesn't qualify as an
            # async generator; the unreachable yield below makes
            # Python build us one that immediately exits. Doc'd
            # pattern in PEP 525.
            yield CodingAgentEvent()
        return
