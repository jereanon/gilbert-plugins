"""Voice-agent plugin — wake-word activated voice conversations.

Registers ``VoiceAgentService`` with the service manager. The service
itself is a wrapper around the core ``voice_brain`` engine — see
``voice_agent_service.py`` for the conversation lifecycle.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


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


def create_plugin() -> Plugin:
    return VoiceAgentPlugin()
