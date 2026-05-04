"""Tests for browser tool dispatch.

The Page is replaced with an AsyncMock; the tests assert against the
calls Playwright would receive.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_browser.browser_service import BrowserService

from gilbert.interfaces.attachments import FileAttachment
from gilbert.interfaces.tools import ToolResult


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
    # browser_extract is gated behind ai_chat — not advertised when missing.
    assert "browser_extract" not in names


@pytest.mark.asyncio
async def test_get_tools_includes_extract_when_ai_sampling_available(tmp_path: Path):
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())
    svc._ai_sampling = MagicMock()  # presence is what matters
    names = {t.name for t in svc.get_tools()}
    assert "browser_extract" in names


@pytest.mark.asyncio
async def test_browser_extract_calls_ai_sampling_with_prompt(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    page.locator.return_value.inner_text = AsyncMock(return_value="page body text")

    fake_message = MagicMock()
    fake_message.content = '{"answer": 42}'
    fake_response = MagicMock()
    fake_response.message = fake_message
    ai = MagicMock()
    ai.complete_one_shot = AsyncMock(return_value=fake_response)
    svc._ai_sampling = ai

    out = await svc.execute_tool(
        "browser_extract",
        {
            "_user_id": "u",
            "instruction": "extract the answer",
            "json_schema": '{"type":"object"}',
        },
    )
    ai.complete_one_shot.assert_awaited_once()
    kwargs = ai.complete_one_shot.call_args.kwargs
    assert "extraction" in kwargs["system_prompt"].lower() or "json" in kwargs["system_prompt"].lower()
    user_msg = kwargs["messages"][0]
    assert "extract the answer" in user_msg.content
    assert "page body text" in user_msg.content
    assert out == '{"answer": 42}'


@pytest.mark.asyncio
async def test_browser_extract_strips_code_fences(tmp_path: Path):
    svc, page = _service_with_page(tmp_path)
    page.locator.return_value.inner_text = AsyncMock(return_value="x")
    fake_resp = MagicMock()
    fake_resp.message.content = "```json\n{\"a\": 1}\n```"
    svc._ai_sampling = MagicMock()
    svc._ai_sampling.complete_one_shot = AsyncMock(return_value=fake_resp)
    out = await svc.execute_tool(
        "browser_extract", {"_user_id": "u", "instruction": "x"}
    )
    assert out.strip() == '{"a": 1}'


@pytest.mark.asyncio
async def test_browser_extract_without_ai_sampling_returns_error(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    svc._ai_sampling = None
    out = await svc.execute_tool(
        "browser_extract", {"_user_id": "u", "instruction": "x"}
    )
    assert "error" in out.lower()
    assert "ai sampling" in out.lower()


@pytest.mark.asyncio
async def test_browser_extract_requires_instruction(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    svc._ai_sampling = MagicMock()
    svc._ai_sampling.complete_one_shot = AsyncMock()
    out = await svc.execute_tool("browser_extract", {"_user_id": "u"})
    assert "instruction" in out.lower()
    svc._ai_sampling.complete_one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_tool_name_raises(tmp_path: Path):
    svc, _ = _service_with_page(tmp_path)
    with pytest.raises(KeyError):
        await svc.execute_tool("browser_does_not_exist", {"_user_id": "u"})
