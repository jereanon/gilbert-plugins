"""Telnyx plugin — registers two backends and two webhook services:

- ``TelnyxTelephony`` (``TelephonyBackend``) + ``TelnyxWebhookService``
  (``telnyx_webhook`` capability) — outbound voice calls, consumed by
  the ``phone`` plugin.
- ``TelnyxMessaging`` (``MessagingBackend``) +
  ``TelnyxMessagingWebhookService`` (``telnyx_messaging_webhook``
  capability) — bidirectional SMS, consumed by the ``messaging``
  plugin.

Both products share the same Telnyx API key (configured separately on
each backend so the keys can be rotated independently). The capability
services keep ``web/`` routes from importing this module directly.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class TelnyxPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="telnyx",
            version="1.0.0",
            description=(
                "Telnyx integration — telephony (voice calls) + "
                "messaging (SMS / MMS). Each product is a separate "
                "backend registered via the standard backend registry."
            ),
            provides=[
                "telnyx_telephony",
                "telnyx_webhook",
                "telnyx_messaging",
                "telnyx_messaging_webhook",
            ],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        # Side-effect imports register both backends via
        # ``__init_subclass__`` on the respective ABCs.
        from . import telnyx_messaging, telnyx_telephony

        context.services.register(telnyx_telephony.TelnyxWebhookService())
        context.services.register(
            telnyx_messaging.TelnyxMessagingWebhookService()
        )

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TelnyxPlugin()
