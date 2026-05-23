"""Voice-agent plugin — wake-word activated voice conversations.

Registers ``VoiceAgentService`` with the service manager. The service
itself is a wrapper around the core ``voice_brain`` engine — see
``voice_agent_service.py`` for the conversation lifecycle.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIRoute,
)


class VoiceAgentPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="voice-agent",
            version="0.1.0",
            description=(
                "Wake-word activated voice conversations driven by the "
                "voice_brain engine."
            ),
            provides=["voice_agent"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .voice_agent_service import VoiceAgentService

        context.services.register(VoiceAgentService())

    async def teardown(self) -> None:
        pass

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/voice",
                panel_id="voice_agent.page",
                label="Voice",
                description=(
                    "Start a real-time voice conversation with Gilbert. "
                    "Press the button, talk; Gilbert speaks back through "
                    "your browser."
                ),
                icon="mic",
                required_role="user",
                # Gate the route on the service capability so disabling
                # the service under Settings → Services hides both the
                # nav entry and the SPA route.
                requires_capability="voice_agent",
                add_to_nav=True,
                # Top-level nav entry (no parent group). Leaving
                # ``nav_parent_group`` blank tells the nav-merge logic
                # in core/services/web_api.py to synthesize a new
                # group keyed off the route's label. Renders as a
                # standalone "Voice" leaf with the mic icon, same
                # shape as Calendar / Feeds / Tasks etc.
            ),
        ]


def create_plugin() -> Plugin:
    return VoiceAgentPlugin()
