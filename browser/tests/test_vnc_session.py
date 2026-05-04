"""Tests for VncSessionManager — process spawning is faked.

Real Xvfb/x11vnc/websockify aren't available in CI; we patch
``asyncio.create_subprocess_exec`` and assert on the commands the
manager would have spawned.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert_plugin_browser.vnc import VncSessionManager


@pytest.fixture
def fake_subprocess(monkeypatch):
    started: list[list[str]] = []

    async def fake_create(*args, **kwargs):
        started.append(list(args))
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return started


@pytest.mark.asyncio
async def test_start_session_spawns_four_procs(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path, max_per_user=2, max_total=5)
    session = await mgr.start_session("u1", target_url="https://example.com/login")
    try:
        assert session.session_id
        assert session.user_id == "u1"
        assert session.websockify_port > 0
        assert session.display >= 90

        # First arg of each command is the executable.
        cmds = [s[0].split("/")[-1] for s in fake_subprocess]
        assert "Xvfb" in cmds
        assert "x11vnc" in cmds
        assert "websockify" in cmds
        # Chromium fallback default is "chromium".
        assert any(c in ("chromium", "google-chrome") for c in cmds)
    finally:
        await mgr.stop_session(session.session_id)


@pytest.mark.asyncio
async def test_target_url_passed_to_chromium(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path)
    target = "https://x.test/login"
    session = await mgr.start_session("u1", target_url=target)
    try:
        chromium_call = next(
            s for s in fake_subprocess if s[0].endswith("chromium")
        )
        assert target in chromium_call
    finally:
        await mgr.stop_session(session.session_id)


@pytest.mark.asyncio
async def test_stop_session_terminates_all_procs(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path)
    session = await mgr.start_session("u1")
    procs = list(session.procs)
    await mgr.stop_session(session.session_id)
    for p in procs:
        p.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_stop_session_is_idempotent(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path)
    session = await mgr.start_session("u1")
    await mgr.stop_session(session.session_id)
    out = await mgr.stop_session(session.session_id)
    assert out is None


@pytest.mark.asyncio
async def test_per_user_cap_is_enforced(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path, max_per_user=1, max_total=10)
    s = await mgr.start_session("u1")
    try:
        with pytest.raises(RuntimeError, match="per user"):
            await mgr.start_session("u1")
    finally:
        await mgr.stop_session(s.session_id)


@pytest.mark.asyncio
async def test_total_cap_is_enforced(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path, max_per_user=99, max_total=2)
    a = await mgr.start_session("u1")
    b = await mgr.start_session("u2")
    try:
        with pytest.raises(RuntimeError, match="server-wide"):
            await mgr.start_session("u3")
    finally:
        await mgr.stop_session(a.session_id)
        await mgr.stop_session(b.session_id)


@pytest.mark.asyncio
async def test_get_session_rejects_other_user(tmp_path: Path, fake_subprocess):
    mgr = VncSessionManager(data_dir=tmp_path)
    s = await mgr.start_session("u1")
    try:
        assert mgr.get_session(s.session_id, "u1") is s
        assert mgr.get_session(s.session_id, "u2") is None
    finally:
        await mgr.stop_session(s.session_id)


@pytest.mark.asyncio
async def test_idle_session_is_reaped(tmp_path: Path, fake_subprocess):
    """Reap-loop drops sessions whose ``last_used`` is older than the
    timeout. We don't patch ``time.monotonic`` globally — that breaks
    asyncio's event-loop clock — instead we backdate ``last_used``
    directly on the session record."""
    mgr = VncSessionManager(data_dir=tmp_path, idle_timeout_seconds=60)
    s = await mgr.start_session("u1")
    # Simulate idleness by making last_used 1000s ago.
    s.last_used = s.last_used - 1000
    await mgr._reap_once()
    assert s.session_id not in mgr._sessions


@pytest.mark.asyncio
async def test_each_session_gets_a_distinct_user_data_dir(
    tmp_path: Path, fake_subprocess
):
    mgr = VncSessionManager(data_dir=tmp_path, max_per_user=10, max_total=10)
    a = await mgr.start_session("u1")
    b = await mgr.start_session("u1")
    try:
        assert a.user_data_dir != b.user_data_dir
        assert a.user_data_dir.exists()
        assert b.user_data_dir.exists()
    finally:
        await mgr.stop_session(a.session_id)
        await mgr.stop_session(b.session_id)
