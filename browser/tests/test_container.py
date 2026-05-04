"""Tests for BrowserContainer (subprocess fakes — no real Docker)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_browser.container import BrowserContainer


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patch asyncio.create_subprocess_exec.

    Each call appends ``(args, kwargs)`` to ``calls`` and returns a fake
    Process whose stdout pipes back the next line from
    ``stdout_replies`` (one per call). Mutate ``stdout_replies`` in
    individual tests to inject specific output.
    """
    calls: list[tuple[tuple, dict]] = []
    stdout_replies: list[bytes] = [b"fakecontainerid\n"]

    async def fake_create(*args, **kwargs):
        calls.append((args, kwargs))
        proc = MagicMock()
        out = stdout_replies.pop(0) if stdout_replies else b""
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return {"calls": calls, "stdout_replies": stdout_replies}


@pytest.fixture
def fake_open_connection(monkeypatch):
    """Make the readiness probe succeed immediately.

    The probe sends a WS upgrade request and looks for ``101`` in the
    response's first line. Fake reader returns a synthetic
    ``HTTP/1.1 101 Switching Protocols`` line so the probe passes
    without an actual WS server.
    """
    async def fake_open(host, port):
        reader = MagicMock()
        reader.readline = AsyncMock(
            return_value=b"HTTP/1.1 101 Switching Protocols\r\n"
        )
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open)


@pytest.mark.asyncio
async def test_start_runs_docker_run_with_launch_server(
    fake_subprocess, fake_open_connection
):
    container = BrowserContainer(image="myorg/playwright:test")
    ws = await container.start()
    assert ws.startswith("ws://127.0.0.1:")
    assert ws.endswith("/")

    # Inspect the docker run invocation.
    args, _ = fake_subprocess["calls"][0]
    assert args[0] == "docker"
    assert args[1] == "run"
    assert "myorg/playwright:test" in args
    # The command runs node with an inline launchServer expression.
    assert args[-3] == "node"
    assert args[-2] == "-e"
    cmd = args[-1]
    assert "launchServer" in cmd
    assert "wsPath" in cmd

    await container.stop()


@pytest.mark.asyncio
async def test_start_failure_raises_and_clears_state(
    monkeypatch, fake_open_connection
):
    async def fake_create(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"image not found"))
        proc.returncode = 1
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    # Pin a manual image tag so _ensure_image_built skips the build
    # path — the test exercises 'docker run failed', not the build.
    container = BrowserContainer(image="myorg/playwright:test")
    with pytest.raises(RuntimeError, match="docker run failed"):
        await container.start()
    assert container.ws_endpoint is None


@pytest.mark.asyncio
async def test_stop_is_idempotent(fake_subprocess, fake_open_connection):
    container = BrowserContainer()
    await container.start()
    await container.stop()
    # Second stop is a no-op.
    await container.stop()


@pytest.mark.asyncio
async def test_image_default_is_pinned_to_playwright_version(
    fake_subprocess, fake_open_connection
):
    container = BrowserContainer()
    # Default is the gilbert-built image, tagged with the installed
    # playwright version. Microsoft's base image is the FROM in the
    # Dockerfile but isn't what containers run as.
    assert container.image.startswith("gilbert-browser:v")
    assert container._pw_version


@pytest.mark.asyncio
async def test_image_build_runs_when_not_cached(monkeypatch, fake_open_connection, tmp_path):
    """``docker image inspect`` non-zero → build runs before docker run."""
    sequence: list[str] = []

    async def fake_create(*args, **kwargs):
        cmd = " ".join(str(a) for a in args)
        sequence.append(cmd)
        proc = MagicMock()
        # First call is ``docker image inspect`` — make it fail so the
        # build path runs. Build call is communicate(); make that
        # succeed. Subsequent docker run / kill use returncode=0.
        if "image inspect" in cmd:
            proc.wait = AsyncMock(return_value=1)
            proc.returncode = 1
        else:
            proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    # Provide a fake build context so _ensure_image_built doesn't
    # bail on missing Dockerfile dir.
    ctx = tmp_path / "docker"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")

    container = BrowserContainer(build_context=ctx)
    await container.start()
    try:
        # Build ran before docker run.
        assert any("image inspect" in c for c in sequence)
        assert any("docker build" in c for c in sequence)
        assert any("docker run" in c for c in sequence)
        # Order: inspect → build → run.
        ins = next(i for i, c in enumerate(sequence) if "image inspect" in c)
        bld = next(i for i, c in enumerate(sequence) if "docker build" in c)
        run = next(i for i, c in enumerate(sequence) if "docker run" in c)
        assert ins < bld < run
    finally:
        await container.stop()


@pytest.mark.asyncio
async def test_image_build_skipped_when_cached(monkeypatch, fake_open_connection):
    """``docker image inspect`` exit-0 → build does NOT run."""
    sequence: list[str] = []

    async def fake_create(*args, **kwargs):
        cmd = " ".join(str(a) for a in args)
        sequence.append(cmd)
        proc = MagicMock()
        if "image inspect" in cmd:
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
        else:
            proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    container = BrowserContainer()
    await container.start()
    try:
        assert any("image inspect" in c for c in sequence)
        assert not any("docker build" in c for c in sequence)
    finally:
        await container.stop()


def test_is_available_false_when_docker_missing(monkeypatch):
    """is_available returns False when ``docker`` is not on PATH."""
    import subprocess

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert BrowserContainer.is_available() is False


def test_is_available_false_when_daemon_down(monkeypatch):
    """is_available returns False when the docker CLI exits non-zero."""
    import subprocess

    def fake_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 1
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert BrowserContainer.is_available() is False


def test_is_available_true_when_daemon_up(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert BrowserContainer.is_available() is True


@pytest.mark.asyncio
async def test_readiness_probe_times_out(monkeypatch, fake_subprocess):
    """If the WS endpoint never becomes reachable, start() raises."""
    async def fake_open(host, port):
        raise OSError("nothing listening")

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    # Use a tiny timeout so the test doesn't sit on the default 60s.
    container = BrowserContainer(ready_timeout=0.5)
    with pytest.raises(RuntimeError, match="did not become ready within"):
        await container.start()
