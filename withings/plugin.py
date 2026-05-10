"""Withings plugin — registers the OAuth pull backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIPanel,
)


class WithingsPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="withings",
            version="1.0.0",
            description=(
                "Withings Public Cloud API OAuth pull backend. Syncs "
                "sleep, weight, blood pressure, and heart rate every "
                "6 hours by default."
            ),
            provides=["withings_health"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import withings_backend  # noqa: F401  — triggers registration

    async def teardown(self) -> None:
        return None

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="withings.account",
                slot="account.extensions",
                label="Withings",
                description="Connect your Withings account via OAuth.",
                required_role="user",
            ),
        ]


def create_plugin() -> Plugin:
    return WithingsPlugin()

