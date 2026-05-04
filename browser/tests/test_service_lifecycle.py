"""Tests for BrowserService start/stop lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert_plugin_browser.browser_service import BrowserService


@pytest.mark.asyncio
async def test_service_starts_and_stops(tmp_path: Path):
    storage = MagicMock()
    svc = BrowserService(data_dir=tmp_path, storage=storage)

    fake_pw = MagicMock()
    fake_pw.chromium.launch = AsyncMock()
    fake_pw_cm = MagicMock()
    fake_pw_cm.start = AsyncMock(return_value=fake_pw)
    fake_pw_cm.stop = AsyncMock()

    with patch(
        "gilbert_plugin_browser.browser_service.async_playwright",
        return_value=fake_pw_cm,
    ):
        resolver = MagicMock()
        resolver.get_capability.return_value = None
        await svc.start(resolver)
        await svc.stop()

    fake_pw_cm.start.assert_awaited_once()
    fake_pw_cm.stop.assert_awaited_once()


def test_service_info_declares_capability(tmp_path: Path):
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())
    info = svc.service_info()
    assert info.name == "browser"
    assert "browser" in info.capabilities
    assert "workspace" in info.optional
