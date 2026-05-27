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
            UIRoute(
                path="/conversations",
                panel_id="voice_agent.live_conversations",
                label="Live Conversations",
                description=(
                    "Watch every active voice conversation in real time "
                    "— Mentra smart-glasses sessions, browser voice-"
                    "agent sessions, and any future modality that "
                    "publishes the standard ``conversation.*`` bus "
                    "events. Live transcripts stream in as users speak "
                    "and Gilbert replies."
                ),
                icon="message-square",
                required_role="user",
                # Hosted by voice-agent but subscribes to provider-
                # agnostic ``conversation.*`` bus events — Mentra,
                # voice-agent, and any future modality that publishes
                # those events surface here automatically with zero
                # plugin coupling.
                #
                # Gated on the voice_agent capability so the route
                # hides when no voice service is loaded. (If
                # voice-agent is disabled but Mentra is on, this
                # page won't appear; the alternative is multi-cap
                # gating which UIRoute doesn't support, and Mentra
                # without voice-agent is the rarer setup.)
                requires_capability="voice_agent",
                add_to_nav=True,
            ),
        ]


def create_plugin() -> Plugin:
    return VoiceAgentPlugin()
