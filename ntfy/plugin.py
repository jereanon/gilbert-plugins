"""ntfy plugin — registers the NtfyPush backend with the
PushNotificationBackend registry."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class NtfyPlugin(Plugin):
    """Side-effect plugin: importing ``ntfy_push`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="ntfy",
            version="1.0.0",
            description="ntfy push-notification backend (ntfy.sh or self-hosted)",
            provides=["ntfy_push"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import ntfy_push  # noqa: F401  — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return NtfyPlugin()

