"""Frigate plugin — registers the FrigateCameraBackend.

Side-effect import inside ``setup()`` triggers
``CameraEventBackend.__init_subclass__`` and registers the backend in
the global registry under ``backend_name="frigate"``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIPanel,
    UIRoute,
)


class FrigatePlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="frigate",
            version="1.0.0",
            description=(
                "Frigate camera-event backend "
                "(MQTT push + HTTP snapshots/clips)"
            ),
            provides=["frigate_camera"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import backend  # noqa: F401 — triggers backend registration

    async def teardown(self) -> None:
        pass

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="frigate.recent_events",
                slot="dashboard.bottom",
                label="Recent camera events",
                required_role="user",
            ),
        ]

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/cameras",
                panel_id="frigate.cameras_page",
                label="Cameras",
                description="Live view of cameras and recent detection events.",
                icon="monitor",
                required_role="user",
                add_to_nav=True,
                nav_parent_group="system",
                show_in_dashboard=True,
            ),
        ]


def create_plugin() -> Plugin:
    return FrigatePlugin()

