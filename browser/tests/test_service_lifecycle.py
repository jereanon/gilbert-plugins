"""Tests for BrowserService start/stop lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gilbert_plugin_browser.browser_service import BrowserService


def _service(tmp_path: Path, lifecycle: str = "eager") -> tuple[BrowserService, MagicMock]:
    """Build a BrowserService with playwright + container fakes wired."""
    storage = MagicMock()
    svc = BrowserService(data_dir=tmp_path, storage=storage)
    # Force host-native mode so the test doesn't try to spin up a real
    # Docker container on machines that have Docker installed.
    svc._mode = "host"
    svc._lifecycle_mode = lifecycle

    fake_pw = MagicMock()
    fake_pw.chromium.launch = AsyncMock()
    fake_pw.chromium.connect = AsyncMock()
    fake_pw_cm = MagicMock()
    fake_pw_cm.start = AsyncMock(return_value=fake_pw)
    fake_pw_cm.stop = AsyncMock()
    return svc, fake_pw_cm


@pytest.mark.asyncio
async def test_eager_lifecycle_blocks_until_pool_ready(tmp_path: Path):
    svc, fake_pw_cm = _service(tmp_path, lifecycle="eager")
    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)
        # By the time start() returned, the pool must be ready.
        assert svc._pool is not None
        fake_pw_cm.start.assert_awaited_once()
        await svc.stop()
    fake_pw_cm.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_lifecycle_returns_before_pool_ready(tmp_path: Path):
    svc, fake_pw_cm = _service(tmp_path, lifecycle="async")
    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)
        # start() returns without awaiting the pool task.
        assert svc._pool_ready_task is not None
        # Wait for the background task to finish so pool ends up ready.
        await svc._pool_ready_task
        assert svc._pool is not None
        await svc.stop()


@pytest.mark.asyncio
async def test_on_demand_lifecycle_does_not_start_pool_in_start(tmp_path: Path):
    svc, fake_pw_cm = _service(tmp_path, lifecycle="on_demand")
    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)
        # No pool, no background task, no playwright started.
        assert svc._pool is None
        assert svc._pool_ready_task is None
        fake_pw_cm.start.assert_not_called()
        # First _ensure_pool_ready brings it up.
        await svc._ensure_pool_ready()
        assert svc._pool is not None
        fake_pw_cm.start.assert_awaited_once()
        await svc.stop()


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_startup_future(tmp_path: Path):
    svc, fake_pw_cm = _service(tmp_path, lifecycle="on_demand")
    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)
        # Fire three _ensure_pool_ready calls concurrently — only one
        # should drive the actual startup (fake_pw_cm.start awaited once).
        await asyncio.gather(
            svc._ensure_pool_ready(),
            svc._ensure_pool_ready(),
            svc._ensure_pool_ready(),
        )
        fake_pw_cm.start.assert_awaited_once()
        await svc.stop()


@pytest.mark.asyncio
async def test_failed_startup_clears_future_so_next_call_retries(tmp_path: Path):
    svc, fake_pw_cm = _service(tmp_path, lifecycle="on_demand")
    # First attempt fails — playwright.start raises.
    fake_pw_cm.start = AsyncMock(side_effect=[RuntimeError("nope"), MagicMock()])
    fake_pw = MagicMock()
    fake_pw.chromium.launch = AsyncMock()
    fake_pw_cm.start.side_effect = [RuntimeError("nope"), fake_pw]

    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)

        with pytest.raises(RuntimeError, match="nope"):
            await svc._ensure_pool_ready()
        # Future is cleared so a retry is possible.
        assert svc._pool_ready_future is None
        # Second attempt succeeds.
        await svc._ensure_pool_ready()
        assert svc._pool is not None
        await svc.stop()


def test_service_info_declares_capability(tmp_path: Path):
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())
    info = svc.service_info()
    assert info.name == "browser"
    assert "browser" in info.capabilities
    assert "workspace" in info.optional
