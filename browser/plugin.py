"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
)

# Actually launch a headless Chromium and close it. This catches both
# missing-binary AND missing-os-deps failures, which a path-only probe
# would miss — Playwright 1.49+ uses a separate ``chromium-headless-shell``
# binary for headless mode, so ``executable_path`` (which resolves to the
# full Chromium) can pass while ``launch(headless=True)`` still fails.
_CHROMIUM_CHECK_PY = (
    "from playwright.sync_api import sync_playwright; "
    "p = sync_playwright().start(); "
    "b = p.chromium.launch(headless=True); "
    "b.close(); p.stop()"
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
                description=(
                    "Headless Chrome binaries used by Playwright "
                    "(~170 MB full chromium + ~80 MB headless shell). "
                    "Check actually launches a headless browser to catch "
                    "missing OS shared libs as well as missing binaries."
                ),
                check_cmd=f"uv run python -c '{_CHROMIUM_CHECK_PY}'",
                # Playwright >= 1.49 uses a separate ``chromium-headless-shell``
                # binary for headless mode. Install both so launch(headless=True)
                # works, and full-chromium launches (e.g. the VNC live-login
                # flow's headed Chromium fallback) keep working too. On Linux,
                # the operator additionally needs the OS shared libs
                # (libatk-1.0.so.0, libnss3, libcups2, …) — those go in via
                # ``playwright install-deps`` which needs sudo, so we don't
                # auto-install them.
                install_hint=(
                    "uv run playwright install chromium chromium-headless-shell"
                    "  AND  sudo uv run playwright install-deps chromium"
                    " (Linux only — installs libatk1.0-0, libnss3, libcups2, …)"
                ),
                auto_install_cmd=(
                    "uv run playwright install chromium chromium-headless-shell"
                ),
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
