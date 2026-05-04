"""BrowserContainer — runs Playwright in Docker, exposes a WS endpoint.

Microsoft maintains ``mcr.microsoft.com/playwright:vX.Y.Z-jammy``
container images that bundle Node.js + Playwright + Chromium + every
OS shared library Chromium needs to launch. Running this container
with ``npx playwright run-server`` exposes a WebSocket Playwright
clients (including Python's) can connect to via
``chromium.connect(ws_endpoint)``.

Putting the browser in a container means the host only needs Docker —
no ``apt-get install libnss3 libatk1.0-0 libcups2 …`` gymnastics, no
``playwright install-deps`` (which needs sudo), no fragile
ldconfig-based dependency probes. Updates are a ``docker pull`` away.

We pin the image tag to the Python ``playwright`` package version
detected at runtime so the wire protocol matches.

Lifecycle:

1. ``BrowserContainer.is_available()`` — ``docker --version`` exits 0.
2. ``await container.start()`` — runs the image with run-server,
   waits for the WS endpoint to accept connections, returns it.
3. Application code calls ``chromium.connect(ws_endpoint)`` and uses
   contexts/pages exactly as if the browser were local. Storage
   state, screenshots, and bytes all flow through the WS protocol —
   no volume mounts needed.
4. ``await container.stop()`` — ``docker kill`` the container.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import subprocess
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger(__name__)


def _detect_playwright_version() -> str:
    """Best-effort detect the installed Python playwright version.

    Falls back to a known-good version if the metadata lookup fails so
    the plugin still starts (an obviously-mismatched image will surface
    at the run-server health check).
    """
    try:
        return version("playwright")
    except PackageNotFoundError:
        return "1.59.0"


def _free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrowserContainer:
    """Manages a single Playwright browser-in-Docker container."""

    def __init__(
        self,
        *,
        image: str | None = None,
        host_port: int = 0,
        run_server_args: tuple[str, ...] = (),
        ready_timeout: float = 60.0,
    ) -> None:
        if image is None:
            image = f"mcr.microsoft.com/playwright:v{_detect_playwright_version()}-jammy"
        self._image = image
        self._host_port = host_port  # 0 → auto-allocate
        self._extra_args = run_server_args
        self._ready_timeout = ready_timeout
        self._container_id: str | None = None
        self._ws_endpoint: str | None = None

    @classmethod
    async def prune_stale(cls) -> None:
        """Remove any leftover ``gilbert.browser=1`` containers.

        Best-effort one-shot cleanup for containers a previous Gilbert
        run leaked (e.g. a crash between docker run and docker rm).
        Failures are logged at debug level — pruning is purely a tidy-up,
        never required for correctness.
        """
        try:
            ls = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                "label=gilbert.browser=1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(ls.communicate(), timeout=5)
        except (TimeoutError, FileNotFoundError, Exception):
            return
        ids = [line.strip() for line in stdout.decode().splitlines() if line.strip()]
        if not ids:
            return
        logger.info("Pruning %d stale gilbert.browser container(s)", len(ids))
        try:
            rm = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                *ids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm.wait(), timeout=10)
        except Exception:
            logger.debug("prune docker rm failed", exc_info=True)

    @classmethod
    def is_available(cls) -> bool:
        """True when the ``docker`` CLI exists and the daemon answers.

        We don't just probe ``docker --version`` — that succeeds even
        when the docker daemon isn't running. ``docker info`` round-trips
        through the daemon, so it gives a true "can we use docker" answer.
        """
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @property
    def ws_endpoint(self) -> str | None:
        return self._ws_endpoint

    @property
    def image(self) -> str:
        return self._image

    async def start(self) -> str:
        """Start the container and return the Playwright WS endpoint.

        Blocks until the server is accepting WS connections, or raises
        ``RuntimeError`` after a 60s health-check timeout.

        We run ``chromium.launchServer({port, host, wsPath})`` via an
        inline Node expression rather than ``playwright run-server``.
        The CLI's ``run-server`` exposes a Selenium-grid-style endpoint
        that's incompatible with ``BrowserType.connect(ws://...)``;
        ``launchServer`` is the API match. ``wsPath: "/"`` gives us a
        fixed endpoint URL so we don't have to scrape the random
        token the JS server would otherwise generate.
        """
        port = self._host_port or _free_tcp_port()
        name = f"gilbert-browser-{secrets.token_hex(4)}"

        # The Microsoft playwright image pins a Playwright version
        # matching the image tag, so the bundled require('playwright')
        # is the right one — no npx re-resolution needed.
        launcher_js = (
            "const { chromium } = require('playwright'); "
            f"chromium.launchServer({{ port: {port}, host: '0.0.0.0', wsPath: '/' }}).then("
            "s => process.stderr.write('READY ' + s.wsEndpoint() + '\\n')"
            ").catch("
            "e => { process.stderr.write('FAIL ' + (e && e.stack || e) + '\\n'); process.exit(1); }"
            ");"
        )

        cmd = [
            "docker",
            "run",
            "-d",
            # Deliberately NO ``--rm`` — when the container's command
            # crashes (which is what 'connection refused for the full
            # readiness timeout' means), --rm wipes the container
            # before we can read its logs, hiding the actual error.
            # ``stop()`` does the cleanup manually with docker rm.
            "--name",
            name,
            # Tag every gilbert-launched container so a startup-time
            # sweep can prune leftovers from a previous crashed run.
            "--label",
            "gilbert.browser=1",
            "-p",
            f"127.0.0.1:{port}:{port}",
            "--init",
            # Microsoft's Playwright image installs the JS package at
            # /ms-playwright-agent/node_modules/playwright. Setting the
            # working directory there makes ``require('playwright')``
            # walk up and find it. NODE_PATH covers older image layouts
            # and any /usr/lib or /usr/local/lib npm-global installs.
            "-w",
            "/ms-playwright-agent",
            "-e",
            (
                "NODE_PATH="
                "/ms-playwright-agent/node_modules:"
                "/home/pwuser/node_modules:"
                "/usr/lib/node_modules:"
                "/usr/local/lib/node_modules"
            ),
            self._image,
            "node",
            "-e",
            launcher_js,
        ]
        logger.info("Starting browser container: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run failed: {stderr.decode(errors='replace').strip()}"
            )
        self._container_id = stdout.decode().strip()
        self._ws_endpoint = f"ws://127.0.0.1:{port}/"

        try:
            await self._wait_for_ready(port, timeout=self._ready_timeout)
        except Exception as exc:
            # The container is up but never started accepting WS — most
            # commonly a bad ``node -e`` invocation that crashed before
            # binding the port. Pull the container's logs so the operator
            # gets the real error in the traceback instead of just
            # "connection refused".
            logs = await self._container_logs()
            await self.stop()
            if logs:
                raise RuntimeError(
                    f"{exc}\n\n--- container logs ---\n{logs}"
                ) from exc
            raise
        return self._ws_endpoint

    async def _container_logs(self) -> str:
        """Best-effort fetch of the container's stdout+stderr.

        Used by ``start()`` when the readiness probe fails so the
        operator can see what the in-container process actually said
        before / instead of binding the port. Returns an empty string
        on any failure; this is purely diagnostic, never blocking.
        """
        if not self._container_id:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "--tail",
                "200",
                self._container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return out.decode(errors="replace").strip()
        except Exception:
            return ""

    async def stop(self) -> None:
        """Stop and remove the container. Idempotent.

        Two phases: ``docker stop`` (graceful, then SIGKILL after
        Docker's default 10s) and ``docker rm`` (cleanup, since we
        skipped ``--rm`` so logs survive a crash).
        """
        if not self._container_id:
            return
        cid = self._container_id
        self._container_id = None
        self._ws_endpoint = None
        for verb in ("stop", "rm"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    verb,
                    "-f" if verb == "rm" else "--time=5",
                    cid,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=15)
            except (TimeoutError, FileNotFoundError):
                logger.warning("docker %s %s timed out / unavailable", verb, cid)
            except Exception:
                logger.exception("docker %s %s failed", verb, cid)

    async def _wait_for_ready(self, port: int, timeout: float = 60.0) -> None:
        """Poll until the in-container Playwright server accepts a WS upgrade.

        TCP-open is necessary but not sufficient — the Node process
        binds the port a moment before launchServer finishes spinning
        up Chromium and starts answering WS upgrades. We send a real
        WS handshake and wait for the ``HTTP/1.1 101`` response so the
        Python client's first ``connect()`` is guaranteed to succeed.

        First-run image pull adds ~5-15s; subsequent starts are <2s.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_err: Exception | None = None
        # Static handshake fields — Sec-WebSocket-Key is a placeholder
        # since we never use the upgraded socket.
        ws_request = (
            b"GET / HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"\r\n"
        )
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except (OSError, ConnectionError) as exc:
                last_err = exc
                await asyncio.sleep(0.5)
                continue
            try:
                writer.write(ws_request)
                await writer.drain()
                response_line = await asyncio.wait_for(
                    reader.readline(), timeout=2.0
                )
                if b"101" in response_line:
                    return
                last_err = RuntimeError(
                    f"unexpected response: {response_line.decode(errors='replace').strip()}"
                )
            except (OSError, ConnectionError, TimeoutError) as exc:
                last_err = exc
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"browser container did not become ready within {timeout:.0f}s "
            f"(last error: {last_err})"
        )
