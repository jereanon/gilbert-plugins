"""Tests for the per-user Playwright BrowserContext pool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_browser.context_pool import ContextPool


def _make_pool(tmp_path: Path, n_contexts: int = 1, idle_timeout: int = 60) -> tuple[ContextPool, MagicMock, list[AsyncMock]]:
    """Build a pool wired to a fake Playwright that hands out N distinct contexts."""
    contexts = [AsyncMock() for _ in range(n_contexts)]
    for ctx in contexts:
        ctx.storage_state = AsyncMock()
        ctx.close = AsyncMock()
    browser = AsyncMock()
    browser.new_context = AsyncMock(side_effect=contexts)
    browser.close = AsyncMock()
    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)
    pw = MagicMock()
    pw.chromium = chromium
    pool = ContextPool(data_dir=tmp_path, playwright=pw, idle_timeout_seconds=idle_timeout)
    return pool, browser, contexts


@pytest.mark.asyncio
async def test_pool_creates_one_context_per_user(tmp_path: Path):
    pool, browser, contexts = _make_pool(tmp_path, n_contexts=2)
    await pool.start()
    try:
        ctx_a1 = await pool.get_for_user("user-a")
        ctx_a2 = await pool.get_for_user("user-a")
        ctx_b = await pool.get_for_user("user-b")
        assert ctx_a1 is ctx_a2
        assert ctx_a1 is not ctx_b
        assert browser.new_context.await_count == 2
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_persists_storage_state_on_stop(tmp_path: Path):
    pool, _browser, contexts = _make_pool(tmp_path, n_contexts=1)
    await pool.start()
    await pool.get_for_user("u1")
    await pool.stop()
    contexts[0].storage_state.assert_awaited()
    args, kwargs = contexts[0].storage_state.call_args
    state_path = Path(kwargs.get("path") or args[0])
    assert state_path == tmp_path / "users" / "u1" / "state.json"


@pytest.mark.asyncio
async def test_pool_loads_existing_storage_state(tmp_path: Path):
    state_dir = tmp_path / "users" / "u1"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "state.json"
    state_file.write_text("{}")

    pool, browser, _ = _make_pool(tmp_path, n_contexts=1)
    await pool.start()
    try:
        await pool.get_for_user("u1")
        # The new_context call should have included storage_state.
        args, kwargs = browser.new_context.call_args
        assert kwargs.get("storage_state") == str(state_file)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_pool_evicts_idle_contexts(tmp_path: Path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: fake_now[0])

    pool, _browser, contexts = _make_pool(tmp_path, n_contexts=1, idle_timeout=60)
    # Don't start the reaper task — we'll drive it manually.
    pool._browser = await pool._pw.chromium.launch(headless=True)

    await pool.get_for_user("u1")
    fake_now[0] += 120  # past idle window
    await pool._reap_once()

    assert "u1" not in pool._entries
    contexts[0].storage_state.assert_awaited()
    contexts[0].close.assert_awaited()


@pytest.mark.asyncio
async def test_pool_connects_to_remote_when_ws_endpoint_set(tmp_path: Path):
    """When ws_endpoint is set, the pool calls chromium.connect() instead
    of chromium.launch()."""
    contexts = [AsyncMock()]
    contexts[0].storage_state = AsyncMock()
    contexts[0].close = AsyncMock()
    browser = AsyncMock()
    browser.new_context = AsyncMock(side_effect=contexts)
    browser.close = AsyncMock()
    chromium = MagicMock()
    chromium.launch = AsyncMock()
    chromium.connect = AsyncMock(return_value=browser)
    pw = MagicMock()
    pw.chromium = chromium

    pool = ContextPool(
        data_dir=tmp_path,
        playwright=pw,
        idle_timeout_seconds=60,
        ws_endpoint="ws://127.0.0.1:9999/",
    )
    await pool.start()
    try:
        chromium.connect.assert_awaited_with("ws://127.0.0.1:9999/")
        chromium.launch.assert_not_called()
        await pool.get_for_user("u1")
        browser.new_context.assert_awaited()
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_get_for_user_rejects_empty_user_id(tmp_path: Path):
    pool, _browser, _ = _make_pool(tmp_path, n_contexts=0)
    await pool.start()
    try:
        with pytest.raises(ValueError):
            await pool.get_for_user("")
    finally:
        await pool.stop()
