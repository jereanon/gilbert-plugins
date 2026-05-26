"""Messaging plugin — bidirectional SMS / text-message orchestration.

Hosts ``MessagingService`` and its ``send_text_message`` AI tool. The
carrier integration is a SEPARATE plugin (e.g. ``std-plugins/telnyx/``
contributes a ``TelnyxMessagingBackend``). This plugin doesn't depend
on any specific carrier — it resolves whatever ``MessagingBackend`` is
registered through the standard backend registry pattern.

The carrier-side inbound webhook routes (e.g. ``/api/telnyx/messages``)
live in core's ``gilbert.web.routes.telnyx_messaging_webhook`` and
dispatch through the ``telnyx_messaging_webhook`` capability the
Telnyx plugin advertises — exact same pattern the voice side uses
for ``telnyx_webhook``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIRoute,
)


class MessagingPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="messaging",
            version="1.0.0",
            description=(
                "Bidirectional text messaging — MessagingService + "
                "send_text_message AI tool. Carrier integration is a "
                "separate plugin (e.g. telnyx)."
            ),
            provides=["messaging"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .messaging_service import MessagingService

        context.services.register(MessagingService())

    async def teardown(self) -> None:
        pass

    def ui_routes(self) -> list[UIRoute]:
        # ``requires_capability="messaging"`` gates BOTH the route and
        # the synthesized nav entry on the service being live.
        # Disabling messaging in Settings → Services hides both.
        return [
            UIRoute(
                path="/messages",
                panel_id="messaging.page",
                label="Messages",
                description=(
                    "Two-way text messages — SMS threads Gilbert can "
                    "read and send on your behalf."
                ),
                icon="message-square-text",
                required_role="user",
                requires_capability="messaging",
                add_to_nav=True,
                show_in_dashboard=True,
            ),
            UIRoute(
                # Deep-link variant for opening a specific thread.
                # Same component; no nav entry.
                path="/messages/:otherNumber",
                panel_id="messaging.page",
                required_role="user",
                requires_capability="messaging",
            ),
        ]


def create_plugin() -> Plugin:
    return MessagingPlugin()
