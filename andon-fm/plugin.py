"""Andon FM plugin — tune in to the AI-hosted internet radio stations.

Stations: Thinking Frequencies (Claude), OpenAIR (GPT), Backlink
Broadcast (Gemini), Grok and Roll (Grok). All four are plain HTTP MP3
streams on Live365's CDN — the same endpoints the andonlabs.com web
player and the Andon FM hardware radio tune in to. We hand those URLs
to the existing speaker service, which routes them to Sonos / local /
browser speakers like any other internet stream.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIRoute


class AndonFmPlugin(Plugin):
    """Registers the Andon FM service and its full-page tuner under Media."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="andon-fm",
            version="1.0.0",
            description=(
                "Andon FM — listen to the four AI-hosted internet radio "
                "stations from Andon Labs through any Gilbert speaker."
            ),
            provides=["andon_fm"],
            requires=["speaker_control"],
        )

    async def setup(self, context: PluginContext) -> None:
        from .andon_fm_service import AndonFmService

        self._service = AndonFmService()
        context.services.register(self._service)

    async def teardown(self) -> None:
        pass  # Service lifecycle handled by ServiceManager.stop_all()

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/media/andon-fm",
                panel_id="andon_fm.page",
                label="Andon FM",
                description=(
                    "Tune in to the four AI-hosted Andon FM radio "
                    "stations — pick a station, pick which speakers "
                    "play it."
                ),
                icon="radio",
                required_role="user",
                add_to_nav=True,
                nav_parent_group="media",
            ),
        ]


def create_plugin() -> Plugin:
    """Entry point called by the plugin loader."""
    return AndonFmPlugin()
