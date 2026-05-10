"""hk-webhook plugin — registers the generic ``HKWebhookBackend``.

Side-effect import inside ``setup()`` triggers the
``HealthBackend.__init_subclass__`` hook and registers the backend
under ``backend_name="hk-webhook"``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIPanel,
)


class HKWebhookPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="hk-webhook",
            version="1.0.0",
            description=(
                "Generic catch-all health-data webhook backend "
                "(iOS Shortcut, Home Assistant, Garmin Connect IQ, "
                "custom Python — anything that can POST JSON)"
            ),
            provides=["hk_webhook_health"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import hk_webhook_backend  # noqa: F401  — triggers registration

    async def teardown(self) -> None:
        return None

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="hk-webhook.account",
                slot="account.extensions",
                label="Generic Health Webhook",
                description=(
                    "Push metrics from any source via a per-user URL."
                ),
                required_role="user",
            ),
        ]


def create_plugin() -> Plugin:
    return HKWebhookPlugin()

