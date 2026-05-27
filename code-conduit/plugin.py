"""Code Conduit plugin entry point.

Registers the OpenCode backend as a side-effect import and starts
the ``CodeConduitService`` that wraps it.

The plugin is intentionally service-shaped (not backend-only)
because the conduit needs a runtime brain — project-alias
resolution, ToolProvider surface for the AI, future inbound-event
fan-out — that doesn't fit the bare-backend pattern.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class CodeConduitPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="code-conduit",
            version="0.1.0",
            description=(
                "Relay messages between Gilbert and a coding agent "
                "(OpenCode, Claude Code) — fire-and-forget conduit"
            ),
            provides=["code_conduit", "opencode"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Side-effect import registers the OpenCode backend in the
        # CodingAgentBackend registry. Add more backends here as
        # they land (claude_code_backend, etc.).
        from . import opencode_backend  # noqa: F401
        from .code_conduit_service import CodeConduitService

        context.services.register(CodeConduitService())

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return CodeConduitPlugin()
