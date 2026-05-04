"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
)

# Probe Playwright's per-user cache for an installed Chromium binary.
# We can't shell out to ``playwright`` directly because it isn't on PATH
# inside the venv from /bin/sh; running it through the python entry
# point is the portable form.
_CHROMIUM_CHECK_PY = (
    "from playwright.sync_api import sync_playwright; "
    "sync_playwright().__enter__().chromium.executable_path"
)


class BrowserPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="browser",
            version="1.0.0",
            description="Headless Chrome browser tools for Gilbert",
            provides=["browser"],
            requires=[],
        )

    def runtime_dependencies(self) -> list[RuntimeDependency]:
        return [
            RuntimeDependency(
                name="chromium",
                description="Headless Chrome binary used by Playwright (~170 MB)",
                check_cmd=f"uv run python -c '{_CHROMIUM_CHECK_PY}'",
                install_hint="uv run playwright install chromium",
                auto_install_cmd="uv run playwright install chromium",
            ),
            RuntimeDependency(
                name="Xvfb",
                description="Virtual X display — used by the VNC live-login flow",
                check_cmd="command -v Xvfb",
                install_hint="apt-get install xvfb (Linux) — only needed for VNC live login",
            ),
            RuntimeDependency(
                name="x11vnc",
                description="VNC server attached to the headed Chromium's Xvfb display",
                check_cmd="command -v x11vnc",
                install_hint="apt-get install x11vnc (Linux) — only needed for VNC live login",
            ),
            RuntimeDependency(
                name="websockify",
                description="TCP→WebSocket bridge fronting x11vnc for the in-browser noVNC client",
                check_cmd="command -v websockify",
                install_hint="apt-get install websockify (Linux) — only needed for VNC live login",
            ),
        ]

    async def setup(self, context: PluginContext) -> None:
        from .browser_service import BrowserService

        service = BrowserService(
            data_dir=context.data_dir,
            storage=context.storage,
        )
        context.services.register(service)
        self._service = service

    async def teardown(self) -> None:
        # Service lifecycle is managed by the ServiceManager via
        # Service.stop(); nothing extra to do at the plugin level.
        pass


def create_plugin() -> Plugin:
    return BrowserPlugin()
