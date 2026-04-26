"""American Standard / Nexia plugin — registers the Nexia thermostat backend.

Side-effect plugin: importing the backend module triggers
``__init_subclass__`` on ``ThermostatBackend`` and auto-registers
``NexiaThermostatBackend`` in the backend registry.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class AmericanStandardPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="american-standard",
            version="1.0.0",
            description=(
                "American Standard / Trane / Nexia thermostat backend "
                "(cloud-based)"
            ),
            provides=["nexia_thermostat"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import nexia_backend  # noqa: F401

        # Persist the per-account device UUID file under the plugin's
        # data dir so we don't trigger Nexia's account-lockout protection
        # by re-registering as a new device on every Gilbert restart.
        nexia_backend.set_plugin_data_dir(context.data_dir)

    async def teardown(self) -> None:
        from .nexia_backend import reset_shared_home

        await reset_shared_home()


def create_plugin() -> Plugin:
    return AmericanStandardPlugin()
