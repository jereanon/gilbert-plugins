"""Mentra plugin entry point.

Registers ``MentraService`` — owns the email→Gilbert-user-id mapping,
opens a WebSocket back to Mentra Cloud per session, and dispatches
glasses events (transcription, button presses) into Gilbert's AI
service. Also exposes the ``mentra_webhook`` capability that core's
``/api/mentra/webhook`` route resolves.

The plugin's UI route is a single ``/mentra`` Settings-adjacent page
that lets the operator manage user mappings and check live session
state. The bulk of the user-facing UI lives ON the glasses (display
layouts + dashboard); the SPA side is admin-only.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIRoute,
)


class MentraPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="mentra",
            version="0.1.0",
            description=(
                "Mentra smart-glasses platform — voice assistant, "
                "glanceable dashboard, and ambient HUD across "
                "Even Realities G1, Vuzix Z100, and Mentra Live."
            ),
            provides=["mentra", "mentra_webhook"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .mentra_service import MentraService

        context.services.register(MentraService())

    async def teardown(self) -> None:
        pass

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/mentra",
                panel_id="mentra.page",
                label="Mentra Glasses",
                description=(
                    "Smart-glasses integration — pair your Mentra "
                    "account, manage user mappings, and watch live "
                    "session activity."
                ),
                icon="glasses",
                required_role="admin",
                requires_capability="mentra",
                add_to_nav=True,
                nav_parent_group="system",
            ),
        ]


def create_plugin() -> Plugin:
    return MentraPlugin()
