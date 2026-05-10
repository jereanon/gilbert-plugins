"""Plex Media Server plugin — registers the PlexBackend.

Side-effect import inside ``setup()`` triggers
``MediaLibraryBackend.__init_subclass__`` and registers the backend in
the global registry under ``backend_name="plex"``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class PlexPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="plex",
            version="1.0.0",
            description="Plex Media Server library + playback backend",
            provides=["plex_media_library"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import plex_backend  # noqa: F401  — triggers registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return PlexPlugin()
