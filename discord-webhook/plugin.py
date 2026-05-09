"""Discord webhook plugin — registers the DiscordWebhookPush backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class DiscordWebhookPlugin(Plugin):
    """Side-effect plugin: importing ``discord_webhook_push`` registers it."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="discord-webhook",
            version="1.0.0",
            description=(
                "Discord channel webhook backend for push notifications "
                "with SSRF prefix validation and Retry-After honouring."
            ),
            provides=["discord_webhook_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import discord_webhook_push  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return DiscordWebhookPlugin()

