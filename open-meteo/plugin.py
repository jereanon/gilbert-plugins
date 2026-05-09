"""Open-Meteo weather plugin — registers the OpenMeteoWeather backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class OpenMeteoPlugin(Plugin):
    """Side-effect plugin: importing ``open_meteo_weather`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="open-meteo",
            version="1.0.0",
            description="Open-Meteo weather backend (no API key required)",
            provides=["open-meteo-weather"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import open_meteo_weather  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return OpenMeteoPlugin()

