"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import (
    Plugin,
    PluginContext,
    PluginMeta,
    RuntimeDependency,
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


# OS shared libs Playwright's chromium needs at runtime on Linux. Same
# list ``playwright install-deps chromium`` puts through apt-get; we
# probe via ``ldconfig -p`` so the check works without invoking apt.
_CHROMIUM_OS_LIBS = [
    "libnss3.so",
    "libnspr4.so",
    "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0",
    "libcups.so.2",
    "libdrm.so.2",
    "libxkbcommon.so.0",
    "libXcomposite.so.1",
    "libXdamage.so.1",
    "libXfixes.so.3",
    "libXrandr.so.2",
    "libgbm.so.1",
    "libpango-1.0.so.0",
    "libcairo.so.2",
    "libasound.so.2",
    "libatspi.so.0",
]
_OS_LIBS_CHECK = " && ".join(
    f"ldconfig -p | grep -q {lib}" for lib in _CHROMIUM_OS_LIBS
)
# Apt package names matching the libs above. Used in the install hint
# so the operator can copy/paste a single sudo apt-get command — no
# need for uv-as-root.
_CHROMIUM_OS_PKGS = (
    "libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 "
    "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
    "libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0"
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
                name="chromium binaries",
                description=(
                    "Full Chromium (~170 MB) + headless-shell (~80 MB) in "
                    "Playwright's per-user cache. Check actually launches "
                    "a headless browser, so missing OS shared libs surface "
                    "here too."
                ),
                check_cmd=f"uv run python -c '{_CHROMIUM_LAUNCH_CHECK_PY}'",
                install_hint=(
                    "uv run playwright install chromium chromium-headless-shell"
                ),
                auto_install_cmd=(
                    "uv run playwright install chromium chromium-headless-shell"
                ),
            ),
            RuntimeDependency(
                name="chromium OS libs (Linux)",
                description=(
                    "Shared libraries Chromium loads at runtime: libnss3, "
                    "libatk-1.0, libcups2, libdrm, libgbm, libpango, …"
                ),
                check_cmd=_OS_LIBS_CHECK,
                # No auto_install_cmd — apt-get needs sudo and shouldn't
                # be invoked unattended. Hint is a copy/paste-ready
                # one-liner that doesn't depend on ``uv`` being on
                # root's PATH.
                install_hint=f"sudo apt-get install -y {_CHROMIUM_OS_PKGS}",
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
