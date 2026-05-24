"""Telnyx telephony plugin — registers TelnyxTelephony as a TelephonyBackend
and exposes ``TelnyxWebhookService`` as the ``telnyx_webhook`` capability
so core's ``/api/telnyx/*`` routes can dispatch without importing this
plugin module directly.

The backend itself only handles call placement + carrier-side audio
plumbing. The conversation brain (STT, LLM, TTS) lives in core's
``PhoneCallService``.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class TelnyxPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="telnyx",
            version="1.0.0",
            description="Telnyx telephony backend for PhoneCallService",
            provides=["telnyx_telephony", "telnyx_webhook"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import telnyx_telephony  # registers backend via __init_subclass__

        context.services.register(telnyx_telephony.TelnyxWebhookService())

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TelnyxPlugin()
