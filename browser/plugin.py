"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class BrowserPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="browser",
            version="1.0.0",
            description="Headless Chrome browser tools for Gilbert",
            provides=["browser"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .browser_service import BrowserService

        service = BrowserService(
            data_dir=context.data_dir,
            storage=context.storage,
        )
        context.services.register(service)
        self._service = service

    async def teardown(self) -> None:
        # Service lifecycle is managed by the ServiceManager via
        # Service.stop(); nothing extra to do at the plugin level.
        pass


def create_plugin() -> Plugin:
    return BrowserPlugin()
