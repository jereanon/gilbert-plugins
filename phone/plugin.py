"""Phone-call plugin — outbound PSTN calls.

Hosts ``PhoneCallService`` and its ``make_phone_call`` AI tool. Used
to live in ``core/services/phone_call.py``; moved into this plugin
during the conversation-engine extraction so the carrier-agnostic
core only carries the generic ``voice_brain`` engine + interfaces.

The Telnyx carrier integration is a SEPARATE plugin
(``std-plugins/telnyx/``). This plugin doesn't depend on Telnyx
directly — it resolves whatever ``TelephonyBackend`` is registered
through the standard backend registry pattern.

The webhook/Media-WS routes Telnyx talks to still live at
``gilbert.web.routes.telnyx_webhooks`` in core. The routes get
mounted at fixed paths during app startup regardless of plugin
load order (Telnyx may POST before the plugin finishes loading on
a restart). The routes do a lazy ``importlib.import_module`` for
the plugin module so the layering stays clean.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class PhonePlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="phone",
            version="1.0.0",
            description=(
                "Outbound phone calls — PhoneCallService + make_phone_call "
                "AI tool. Carrier integration is a separate plugin (telnyx)."
            ),
            provides=["phone_calls"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .phone_call import PhoneCallService

        context.services.register(PhoneCallService())

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return PhonePlugin()
