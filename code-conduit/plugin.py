"""Code Conduit plugin entry point.

Registers the OpenCode + Claude Code backends as side-effect
imports, starts the ``CodeConduitService`` that wraps them, and
declares the ``/coding`` SPA page that surfaces inbound events +
the outbound compose form.

The plugin is intentionally service-shaped (not backend-only)
because the conduit needs a runtime brain — project-alias
resolution, ToolProvider surface for the AI, inbound-event
fan-out — that doesn't fit the bare-backend pattern.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIRoute


class CodeConduitPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="code-conduit",
            version="0.3.0",
            description=(
                "Relay messages between Gilbert and a coding agent "
                "(OpenCode, Claude Code) — fire-and-forget conduit"
            ),
            provides=["code_conduit", "opencode", "claude_code"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Side-effect imports register each backend in the
        # CodingAgentBackend registry. The service picks one at
        # start time via the ``backend`` config param.
        from . import claude_code_backend, opencode_backend  # noqa: F401
        from .code_conduit_service import CodeConduitService

        context.services.register(CodeConduitService())

    async def teardown(self) -> None:
        pass

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/coding",
                panel_id="code_conduit.page",
                label="Coding",
                description=(
                    "Live feed of coding-agent activity — what the "
                    "agent finished, what errored, what's waiting "
                    "on you — plus a compose form to fire a fresh "
                    "relay without a chat turn."
                ),
                icon="terminal",
                required_role="user",
                requires_capability="code_conduit",
                add_to_nav=True,
                nav_parent_group="system",
            ),
        ]


def create_plugin() -> Plugin:
    return CodeConduitPlugin()
