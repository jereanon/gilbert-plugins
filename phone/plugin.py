"""Phone-call plugin — outbound PSTN calls.

Hosts ``PhoneCallService`` and its ``make_phone_call`` AI tool. Used
to live in ``core/services/phone_call.py``; moved into this plugin
during the conversation-engine extraction so the carrier-agnostic
core only carries the generic ``voice_brain`` engine + interfaces.

The Telnyx carrier integration is a SEPARATE plugin
(``std-plugins/telnyx/``). This plugin doesn't depend on Telnyx
directly — it resolves whatever ``TelephonyBackend`` is registered
through the standard backend registry pattern.

The carrier-side webhook routes (``/api/telnyx/*``) live in core's
``gilbert.web.routes.telnyx_webhooks`` and dispatch through the
``telnyx_webhook`` capability the Telnyx plugin advertises.
"""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    UIRoute,
)


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

    def ui_routes(self) -> list[UIRoute]:
        # Two paths, one component: the list view at ``/calls`` and
        # the per-call detail view at ``/calls/:callId``. The
        # component reads the optional ``callId`` route param via
        # ``useParams()`` so we register one panel for both routes.
        # ``requires_capability="phone_calls"`` gates the route on
        # the service being live, so disabling phone under Settings
        # → Services hides both the nav entry and the SPA route.
        return [
            UIRoute(
                path="/calls",
                panel_id="phone.calls-page",
                label="Calls",
                description=(
                    "Outbound calls Gilbert places on your behalf"
                ),
                icon="phone",
                required_role="user",
                requires_capability="phone_calls",
                add_to_nav=True,
                show_in_dashboard=True,
            ),
            UIRoute(
                # The :callId variant doesn't add a nav entry — same
                # page, just deep-linked to a specific call.
                path="/calls/:callId",
                panel_id="phone.calls-page",
                required_role="user",
                requires_capability="phone_calls",
            ),
        ]


def create_plugin() -> Plugin:
    return PhonePlugin()
