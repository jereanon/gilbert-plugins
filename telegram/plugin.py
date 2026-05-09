"""Telegram plugin — registers the TelegramPush backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class TelegramPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="telegram",
            version="1.0.0",
            description=(
                "Telegram bot push backend with chat-id discovery action. "
                "Polling-mode bots only — webhook-mode bots are rejected "
                "on initialise."
            ),
            provides=["telegram_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import telegram_push  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TelegramPlugin()

