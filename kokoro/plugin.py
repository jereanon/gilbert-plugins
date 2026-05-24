"""Kokoro TTS plugin — registers the KokoroTTSBackend backend."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
)


class KokoroPlugin(Plugin):
    """Side-effect plugin: importing ``kokoro_tts`` registers the backend."""

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="kokoro",
            version="1.0.0",
            description="Kokoro local TTS backend (open-weights, in-process)",
            provides=["kokoro_tts"],
            requires=[],
        )

    def runtime_dependencies(self) -> list[RuntimeDependency]:
        # Exercise the full stack (kokoro + torch + av) with a tiny synth.
        # The python -c string imports both libraries and runs one phoneme
        # through a KPipeline so a misconfigured torch/CUDA/libgomp install
        # fails here instead of at first user request.
        probe = (
            'python -c "import av, kokoro; '
            "p = kokoro.KPipeline(lang_code='a'); "
            "list(p('hi', voice='af_heart'))\""
        )
        return [
            RuntimeDependency(
                name="kokoro-tts stack",
                description=(
                    "Verifies torch + kokoro + PyAV import and that a "
                    "minimal end-to-end synthesis completes."
                ),
                check_cmd=probe,
                install_hint=(
                    "Enable the kokoro plugin (default-disabled) so "
                    "`uv sync` resolves kokoro, torch, and av. First "
                    "synthesis downloads the ~327MB Kokoro-82M model."
                ),
                auto_install_cmd="",
            ),
        ]

    async def setup(self, context: PluginContext) -> None:
        # Importing the module triggers TTSBackend.__init_subclass__,
        # registering "kokoro" in the backend registry.
        from . import kokoro_tts  # noqa: F401

    async def teardown(self) -> None:
        pass


def create_plugin() -> Plugin:
    return KokoroPlugin()
