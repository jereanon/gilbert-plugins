"""Jellyfin Media Server plugin — registers the JellyfinBackend.

Side-effect import inside ``setup()`` triggers
``MediaLibraryBackend.__init_subclass__`` and registers the backend in
the global registry under ``backend_name="jellyfin"``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class JellyfinPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="jellyfin",
            version="1.0.0",
            description="Jellyfin Media Server library + playback backend",
            provides=["jellyfin_media_library"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import jellyfin_backend  # noqa: F401  — triggers registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return JellyfinPlugin()
