"""Tests for browser tool dispatch.

The Page is replaced with an AsyncMock; the tests assert against the
calls Playwright would receive.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.tools import ToolResult
from gilbert_plugin_browser.browser_service import BrowserService


def _service_with_page(tmp_path: Path) -> tuple[BrowserService, AsyncMock]:
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())
    page = AsyncMock()
    page.url = "https://example.com/"
    page.title = AsyncMock(return_value="Example Domain")
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<html><body>Hi</body></html>")
    page.screenshot = AsyncMock(return_value=b"\x89PNG fake")
    body = AsyncMock()
    body.inner_text = AsyncMock(return_value="  hello  \n\n  world  ")
    body.inner_html = AsyncMock(return_value="<p>Hi</p>")
    body.click = AsyncMock()
    body.fill = AsyncMock()
    body.select_option = AsyncMock()
    page.locator = MagicMock(return_value=body)
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    svc._pages["u"] = page

    pool = AsyncMock()
    pool.get_for_user = AsyncMock(return_value=AsyncMock())
    svc._pool = pool
    return svc, page


@pytest.mark.asyncio
async def test_navigate_returns_title_and_url(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    out = await svc.execute_tool(
        "browser_navigate",
        {"url": "https://example.com/", "_user_id": "u"},
    )
    page.goto.assert_awaited_with("https://example.com/", wait_until="load", timeout=30000)
    assert "Example Domain" in out
    assert "https://example.com/" in out


@pytest.mark.asyncio
async def test_navigate_rejects_missing_user(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    out = await svc.execute_tool("browser_navigate", {"url": "https://x.test/"})
    assert "user context" in out.lower()


@pytest.mark.asyncio
async def test_navigate_rejects_empty_url(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    out = await svc.execute_tool("browser_navigate", {"url": "", "_user_id": "u"})
    assert "url" in out.lower()


@pytest.mark.asyncio
async def test_get_text_collapses_whitespace_and_truncates(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    out = await svc.execute_tool("browser_get_text", {"_user_id": "u"})
    assert "hello" in out
    assert "world" in out
    assert out.index("hello") < out.index("world")
    assert "  " not in out  # no double-spaces left

    page.locator.return_value.inner_text = AsyncMock(return_value="x" * 60_000)
    out2 = await svc.execute_tool("browser_get_text", {"_user_id": "u"})
    assert "[truncated]" in out2
    assert len(out2) <= 50_100


@pytest.mark.asyncio
async def test_get_html_with_selector_uses_locator(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    out = await svc.execute_tool(
        "browser_get_html", {"selector": "#main", "_user_id": "u"}
    )
    page.locator.assert_called_with("#main")
    assert "<p>Hi</p>" in out


@pytest.mark.asyncio
async def test_get_html_without_selector_returns_full_document(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    out = await svc.execute_tool("browser_get_html", {"_user_id": "u"})
    page.content.assert_awaited()
    assert "<body>Hi</body>" in out


@pytest.mark.asyncio
async def test_screenshot_returns_image_attachment(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    workspace = MagicMock()
    workspace.get_output_dir = MagicMock(return_value=out_dir)
    workspace.register_file = AsyncMock(return_value={"_id": "file-123"})
    svc._workspace = workspace

    result = await svc.execute_tool(
        "browser_screenshot",
        {"_user_id": "u", "_conversation_id": "conv-1"},
    )
    assert isinstance(result, ToolResult)
    assert len(result.attachments) == 1
    att: FileAttachment = result.attachments[0]
    assert att.kind == "image"
    assert att.media_type == "image/png"
    assert att.workspace_skill == "workspace"
    assert att.workspace_conv == "conv-1"
    assert att.workspace_file_id == "file-123"
    assert att.workspace_path.startswith("outputs/")
    # File was actually written.
    written = out_dir / att.name
    assert written.exists()
    assert written.read_bytes() == b"\x89PNG fake"


@pytest.mark.asyncio
async def test_screenshot_without_workspace_returns_error(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    svc._workspace = None
    result = await svc.execute_tool(
        "browser_screenshot",
        {"_user_id": "u", "_conversation_id": "conv-1"},
    )
    if isinstance(result, ToolResult):
        assert result.is_error
        assert "workspace" in result.content.lower()
    else:
        assert "workspace" in result.lower()


@pytest.mark.asyncio
async def test_screenshot_without_conversation_returns_error(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    svc._workspace = MagicMock()
    out = await svc.execute_tool("browser_screenshot", {"_user_id": "u"})
    out_str = out.content if isinstance(out, ToolResult) else out
    assert "conversation" in out_str.lower()


@pytest.mark.asyncio
async def test_get_tools_returns_definitions(tmp_path: Path):
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert "browser_navigate" in names
    assert "browser_get_text" in names
    assert "browser_get_html" in names
    assert "browser_screenshot" in names


@pytest.mark.asyncio
async def test_unknown_tool_name_raises(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    with pytest.raises(KeyError):
        await svc.execute_tool("browser_does_not_exist", {"_user_id": "u"})
