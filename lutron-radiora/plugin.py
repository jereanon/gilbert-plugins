"""Lutron RadioRA plugin — registers Lutron lights + shades backends.

Side-effect plugin: importing the backend modules triggers
``__init_subclass__`` on ``LightsBackend`` / ``ShadesBackend`` and
auto-registers ``LutronLights`` and ``LutronShades`` in their
respective backend registries.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class LutronPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="lutron-radiora",
            version="1.0.0",
            description="Lutron RadioRA 2 / HomeWorks lights + shades backends",
            provides=["lutron_lights", "lutron_shades"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import lutron_lights, lutron_shades  # noqa: F401

    async def teardown(self) -> None:
        from .bridge import reset_shared_bridge

        await reset_shared_bridge()


def create_plugin() -> Plugin:
    return LutronPlugin()
