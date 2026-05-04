"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
    UIPanel,
)

# Actually launch a headless browser. Catches missing OS shared libs
# (libatk1.0-0, libnss3, libcups2, …) that a path-only check would
# miss, and also catches the chromium-headless-shell binary going
# missing — Playwright >= 1.49 launches a separate, smaller binary for
# headless mode that's installed via ``playwright install
# chromium-headless-shell`` rather than ``playwright install chromium``.
_CHROMIUM_LAUNCH_CHECK_PY = (
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
                name="docker (preferred)",
                description=(
                    "Docker daemon, used to run Playwright + Chromium "
                    "in a container with all OS deps baked in. With "
                    "Docker the host stays clean — no playwright "
                    "install-deps / sudo apt-get gymnastics. The "
                    "browser plugin uses Docker by default when "
                    "available; falls back to host-native Playwright "
                    "if not."
                ),
                check_cmd="docker info",
                install_hint=(
                    "Install Docker Desktop (macOS/Windows) or docker.io "
                    "(Linux: sudo apt-get install docker.io). Then add "
                    "your user to the docker group so the daemon is "
                    "reachable without sudo: "
                    "'sudo usermod -aG docker $USER' and re-login."
                ),
            ),
            RuntimeDependency(
                name="chromium (host-native fallback)",
                description=(
                    "Only needed when Docker isn't available — the "
                    "plugin falls back to launching Chromium on the "
                    "host. Check actually launches a headless browser "
                    "to surface missing OS shared libs alongside missing "
                    "binaries."
                ),
                check_cmd=f"uv run python -c '{_CHROMIUM_LAUNCH_CHECK_PY}'",
                install_hint=(
                    "If you can install Docker, prefer that and ignore "
                    "this check. Otherwise: 'uv run playwright install "
                    "chromium chromium-headless-shell' for the "
                    "binaries, then install Chromium's OS shared libs "
                    "using your system's package manager (see "
                    "https://playwright.dev/python/docs/browsers#install-system-dependencies)."
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

    def ui_panels(self) -> list[UIPanel]:
        return [
            UIPanel(
                panel_id="browser.credentials",
                slot="account.extensions",
                label="Browser logins",
                description=(
                    "Manage saved logins the browser plugin uses to "
                    "sign you into sites without the agent ever seeing "
                    "your password."
                ),
                required_role="user",
            ),
            # Invisible always-mounted host for the VNC modal so the
            # 'browser.vnc' agent-action handler can pop it from any
            # page. Renders nothing visible; just listens for the
            # custom event the action handler dispatches.
            UIPanel(
                panel_id="browser.vnc-mounter",
                slot="app.background",
                label="",
                description="",
                required_role="user",
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
