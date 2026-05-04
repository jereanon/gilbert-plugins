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

        Blocks until the run-server is accepting connections, or raises
        ``RuntimeError`` after a 60s health-check timeout.
        """
        port = self._host_port or _free_tcp_port()
        name = f"gilbert-browser-{secrets.token_hex(4)}"

        # ``run-server`` is a Node CLI inside the JS Playwright package.
        # We let the image's bundled npx invoke it; pinning ``--package``
        # ensures the in-container Playwright matches the image tag's
        # version (npx may otherwise re-resolve a newer one).
        version_str = self._image.rsplit(":", 1)[-1].lstrip("v").split("-")[0]
        run_server_cmd = (
            f"npx -y --package=playwright@{version_str} "
            f"playwright run-server --host 0.0.0.0 --port {port}"
        )
        if self._extra_args:
            run_server_cmd += " " + " ".join(self._extra_args)

        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:{port}",
            "--init",
            self._image,
            "/bin/sh",
            "-c",
            run_server_cmd,
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
        except Exception:
            await self.stop()
            raise
        return self._ws_endpoint

    async def stop(self) -> None:
        """Stop and remove the container. Idempotent."""
        if not self._container_id:
            return
        cid = self._container_id
        self._container_id = None
        self._ws_endpoint = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                cid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (TimeoutError, FileNotFoundError):
            logger.warning("docker kill %s timed out / unavailable", cid)
        except Exception:
            logger.exception("docker kill %s failed", cid)

    async def _wait_for_ready(self, port: int, timeout: float = 60.0) -> None:
        """Poll the host port until something accepts a TCP connection.

        The Microsoft image takes ~5-15 seconds to fetch / extract / boot
        the JS Playwright server on first run; later launches reuse the
        cached image and come up in <2 seconds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_err: Exception | None = None
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            except (OSError, ConnectionError) as exc:
                last_err = exc
                await asyncio.sleep(0.5)
        raise RuntimeError(
            f"browser container did not start within {timeout:.0f}s "
            f"(last error: {last_err})"
        )
