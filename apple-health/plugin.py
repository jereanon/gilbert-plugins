"""apple-health plugin — registers the Apple Health webhook backend.

Side-effect import inside ``setup()`` triggers the
``HealthBackend.__init_subclass__`` hook and registers the backend
under ``backend_name="apple-health"``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIPanel,
)


class AppleHealthPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="apple-health",
            version="1.0.0",
            description=(
                "Apple Health (HealthKit) push backend via an iOS Shortcut "
                "webhook. Translates HealthKit identifier names to "
                "MetricType values; the Shortcut handles sleep-session "
                "boundaries and source-filtering on the device."
            ),
            provides=["apple_health"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import apple_health_backend  # noqa: F401  — triggers registration

    async def teardown(self) -> None:
        return None

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="apple-health.account",
                slot="account.extensions",
                label="Apple Health",
                description=(
                    "Push HealthKit data from your iPhone via an iOS Shortcut."
                ),
                required_role="user",
            ),
        ]


def create_plugin() -> Plugin:
    return AppleHealthPlugin()

