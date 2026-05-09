"""Pushover plugin — registers the PushoverPush backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class PushoverPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="pushover",
            version="1.0.0",
            description=(
                "Pushover push-notification backend (admin app token + "
                "per-user user_key)."
            ),
            provides=["pushover_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import pushover_push  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return PushoverPlugin()

