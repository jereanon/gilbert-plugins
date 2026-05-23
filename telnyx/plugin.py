"""Telnyx telephony plugin — registers TelnyxTelephony as a TelephonyBackend.

Pure side-effect plugin. The backend module's ``__init_subclass__`` hook
runs at import time and slots ``TelnyxTelephony`` into the registry so
``PhoneCallService`` discovers it via
``TelephonyBackend.registered_backends()``.

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
            provides=["telnyx_telephony"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from . import telnyx_telephony  # noqa: F401 — registers backend

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return TelnyxPlugin()
