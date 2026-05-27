"""Tests for AnthropicAI backend — message translation and response parsing."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_anthropic.anthropic_ai import AnthropicAI

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)


@pytest.fixture
def backend() -> AnthropicAI:
    return AnthropicAI()


# --- Initialization ---


async def test_initialize_requires_api_key(backend: AnthropicAI) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_initialize_creates_client(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None
    await backend.close()


async def test_initialize_custom_model(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test", "model": "claude-opus-4-20250514"})
    assert backend._model == "claude-opus-4-20250514"
    await backend.close()


async def test_close_clears_client(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})
    await backend.close()
    assert backend._client is None


# --- Request Building ---


def test_build_messages_user() -> None:
    backend = AnthropicAI()
    messages = [Message(role=MessageRole.USER, content="Hello")]
    result = backend._build_messages(messages)
    assert result == [{"role": "user", "content": "Hello"}]


def test_build_messages_user_with_image_attachment() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="what is this?",
            attachments=[
                FileAttachment(kind="image", media_type="image/png", data="AAAA"),
                FileAttachment(kind="image", media_type="image/jpeg", data="BBBB"),
            ],
        )
    ]
    result = backend._build_messages(messages)
    assert len(result) == 1
    assert result[0]["role"] == "user"
    content = result[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBBB"},
    }
    assert content[2] == {"type": "text", "text": "what is this?"}


def test_build_messages_user_image_without_text() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="",
            attachments=[
                FileAttachment(kind="image", media_type="image/png", data="AAAA"),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["type"] == "image"


def test_build_messages_user_with_document_attachment() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="summarize",
            attachments=[
                FileAttachment(
                    kind="document",
                    name="report.pdf",
                    media_type="application/pdf",
                    data="PDFBYTES",
                ),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert content[0] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": "PDFBYTES",
        },
    }
    assert content[1] == {"type": "text", "text": "summarize"}


def test_build_messages_user_with_text_attachment() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="explain",
            attachments=[
                FileAttachment(
                    kind="text",
                    name="notes.md",
                    media_type="text/markdown",
                    text="# hello world",
                ),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert content[0] == {"type": "text", "text": "## notes.md\n\n# hello world"}
    assert content[1] == {"type": "text", "text": "explain"}


def test_build_messages_mixed_attachment_ordering() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="compare",
            attachments=[
                FileAttachment(
                    kind="text",
                    name="notes.md",
                    media_type="text/markdown",
                    text="text body",
                ),
                FileAttachment(
                    kind="document",
                    name="r.pdf",
                    media_type="application/pdf",
                    data="PDF",
                ),
                FileAttachment(kind="image", media_type="image/png", data="IMG"),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    # Order is image, document, text, user prompt — regardless of the
    # order the attachments were declared in.
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "document"
    assert content[2] == {"type": "text", "text": "## notes.md\n\ntext body"}
    assert content[3] == {"type": "text", "text": "compare"}


def test_build_messages_user_with_opaque_file_attachment_reference() -> None:
    """Reference-mode ``kind="file"`` attachments (the normal shape
    after the HTTP upload endpoint lands them on disk) render as a
    text stub naming the file + size + mime type AND telling the
    model exactly how to reach the file: workspace coords and the
    ``run_workspace_script`` tool with ``skill_name='chat-uploads'``.
    The contents are NOT uploaded to Anthropic — a 332 MB STEP file
    would blow out the context window and Anthropic can't parse it
    anyway. The AI is expected to write a Python script that opens
    the file by its bare relative path and prints a summary."""
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="count the lines in this",
            attachments=[
                FileAttachment(
                    kind="file",
                    name="SeaArk_Hull_Model_V4.step",
                    media_type="application/octet-stream",
                    workspace_skill="chat-uploads",
                    workspace_path="SeaArk_Hull_Model_V4.step",
                    workspace_conv="conv-abc",
                    size=332 * 1024 * 1024,  # 332 MB
                ),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert len(content) == 2
    stub = content[0]
    assert stub["type"] == "text"
    # Basic file metadata.
    assert "SeaArk_Hull_Model_V4.step" in stub["text"]
    assert "application/octet-stream" in stub["text"]
    assert "332.0 MB" in stub["text"]
    # The stub tells the model how to analyze it — via the
    # script-running tool, not by asking for another format.
    assert "run_workspace_script" in stub["text"]
    assert "chat-uploads" in stub["text"]
    # And that it shouldn't try to slurp the whole file.
    assert "Do NOT" in stub["text"] or "do not" in stub["text"].lower()
    # User's typed message comes last.
    assert content[1] == {"type": "text", "text": "count the lines in this"}


def test_build_messages_user_with_opaque_file_attachment_inline_legacy() -> None:
    """Legacy inline-mode file attachments (no workspace coords)
    still render as a stub but with a note saying there's no disk
    path to run scripts against."""
    import base64

    backend = AnthropicAI()
    payload = base64.b64encode(b"x" * 4096).decode()
    messages = [
        Message(
            role=MessageRole.USER,
            content="what's in this?",
            attachments=[
                FileAttachment(
                    kind="file",
                    name="archive.zip",
                    media_type="application/zip",
                    data=payload,
                ),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert len(content) == 2
    stub = content[0]
    assert "archive.zip" in stub["text"]
    assert "application/zip" in stub["text"]
    assert "4.0 KB" in stub["text"]
    # Legacy path — no workspace coords.
    assert "inline legacy" in stub["text"].lower()
    assert content[1] == {"type": "text", "text": "what's in this?"}


def test_build_messages_file_ordered_after_images_and_docs() -> None:
    """File stubs come after image/document/text blocks but before
    the user's typed message, matching the ordering convention."""
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="check these",
            attachments=[
                FileAttachment(
                    kind="file",
                    name="binary.dat",
                    media_type="application/octet-stream",
                    data="AAAA",
                ),
                FileAttachment(kind="image", media_type="image/png", data="IMG"),
                FileAttachment(
                    kind="text",
                    name="notes.md",
                    media_type="text/markdown",
                    text="hi",
                ),
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "text"
    assert "notes.md" in content[1]["text"]
    # File stub lives after image/document/text, before user prompt.
    assert content[2]["type"] == "text"
    assert "binary.dat" in content[2]["text"]
    assert content[3] == {"type": "text", "text": "check these"}


def test_build_messages_assistant_text_only() -> None:
    backend = AnthropicAI()
    messages = [Message(role=MessageRole.ASSISTANT, content="Hi there")]
    result = backend._build_messages(messages)
    assert result == [{"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]}]


def test_build_messages_assistant_with_tool_calls() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.ASSISTANT,
            content="Let me check.",
            tool_calls=[
                ToolCall(
                    tool_call_id="tc_1",
                    tool_name="search",
                    arguments={"q": "test"},
                )
            ],
        )
    ]
    result = backend._build_messages(messages)
    content = result[0]["content"]
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "Let me check."}
    assert content[1] == {
        "type": "tool_use",
        "id": "tc_1",
        "name": "search",
        "input": {"q": "test"},
    }


def test_build_messages_tool_result() -> None:
    backend = AnthropicAI()
    messages = [
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(tool_call_id="tc_1", content="found it"),
                ToolResult(tool_call_id="tc_2", content="failed", is_error=True),
            ],
        )
    ]
    result = backend._build_messages(messages)
    assert result[0]["role"] == "user"
    content = result[0]["content"]
    assert len(content) == 2
    assert content[0] == {
        "type": "tool_result",
        "tool_use_id": "tc_1",
        "content": "found it",
    }
    assert content[1] == {
        "type": "tool_result",
        "tool_use_id": "tc_2",
        "content": "failed",
        "is_error": True,
    }


def test_build_messages_skips_system() -> None:
    backend = AnthropicAI()
    messages = [
        Message(role=MessageRole.SYSTEM, content="You are helpful"),
        Message(role=MessageRole.USER, content="Hi"),
    ]
    result = backend._build_messages(messages)
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_build_tools() -> None:
    tools = [
        ToolDefinition(
            name="search",
            description="Search for things",
            parameters=[
                ToolParameter(
                    name="query",
                    type=ToolParameterType.STRING,
                    description="Search query",
                ),
            ],
        ),
    ]
    result = AnthropicAI._build_tools(tools)
    assert len(result) == 1
    assert result[0]["name"] == "search"
    assert result[0]["description"] == "Search for things"
    assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"


def test_build_request_body_includes_system() -> None:
    """System prompt arrives as a content-block list (not a bare
    string) so the cache_control marker can attach to it. This is the
    prompt-caching prerequisite: a bare string can't carry a
    cache_control marker, and without that marker the system prompt
    isn't part of the cached prefix."""
    backend = AnthropicAI()
    backend._model = "test-model"
    backend._max_tokens = 100
    backend._temperature = 0.3
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        system_prompt="Be helpful",
    )
    body = backend._build_request_body(request)
    assert isinstance(body["system"], list)
    assert len(body["system"]) == 1
    assert body["system"][0]["type"] == "text"
    assert body["system"][0]["text"] == "Be helpful"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.3


def test_build_request_body_omits_empty_system() -> None:
    backend = AnthropicAI()
    backend._model = "test-model"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "system" not in body


def test_build_request_body_omits_empty_tools() -> None:
    backend = AnthropicAI()
    backend._model = "test-model"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert "tools" not in body


# --- Response Parsing ---


def test_parse_text_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Hello!"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.message.role == MessageRole.ASSISTANT
    assert response.model == "claude-test"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


def test_parse_tool_use_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [
            {"type": "text", "text": "Checking..."},
            {
                "type": "tool_use",
                "id": "tu_123",
                "name": "search",
                "input": {"q": "weather"},
            },
        ],
        "model": "claude-test",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Checking..."
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].tool_call_id == "tu_123"
    assert response.message.tool_calls[0].tool_name == "search"
    assert response.message.tool_calls[0].arguments == {"q": "weather"}
    assert response.stop_reason == StopReason.TOOL_USE


def test_parse_max_tokens_response() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Truncated..."}],
        "model": "claude-test",
        "stop_reason": "max_tokens",
    }
    response = backend._parse_response(data)
    assert response.stop_reason == StopReason.MAX_TOKENS


def test_parse_no_usage() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Hi"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
    }
    response = backend._parse_response(data)
    assert response.usage is None


def test_parse_response_populates_cache_tokens() -> None:
    backend = AnthropicAI()
    data = {
        "content": [{"type": "text", "text": "Hi"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 500,
        },
    }
    response = backend._parse_response(data)
    assert response.usage is not None
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 50
    assert response.usage.cache_creation_tokens == 200
    assert response.usage.cache_read_tokens == 500


def test_parse_multiple_tool_calls() -> None:
    backend = AnthropicAI()
    data = {
        "content": [
            {"type": "tool_use", "id": "tc_1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "tc_2", "name": "b", "input": {"x": 1}},
        ],
        "model": "claude-test",
        "stop_reason": "tool_use",
    }
    response = backend._parse_response(data)
    assert len(response.message.tool_calls) == 2
    assert response.message.tool_calls[0].tool_name == "a"
    assert response.message.tool_calls[1].tool_name == "b"


# --- Generate (integration with mock HTTP) ---


async def test_generate_calls_api(backend: AnthropicAI) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "content": [{"type": "text", "text": "API response"}],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    mock_response.raise_for_status = MagicMock()

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
        system_prompt="Be helpful",
    )
    response = await backend.generate(request)

    assert response.message.content == "API response"
    backend._client.post.assert_called_once()
    call_kwargs = backend._client.post.call_args
    assert call_kwargs[0][0] == "/messages"

    await backend.close()


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: AnthropicAI,
) -> None:
    """A 4xx response should surface Anthropic's error.message, not opaque HTTP text."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 400
    mock_response.json.return_value = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "messages.49: all messages must have non-empty content",
        },
    }

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
    )

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 400
    assert "messages.49: all messages must have non-empty content" in str(exc_info.value)
    assert "400" in str(exc_info.value)

    await backend.close()


def test_build_messages_splits_slash_command_combined_row(backend: AnthropicAI) -> None:
    """Assistant rows carrying both tool_calls and tool_results must be split.

    Slash-command turns are persisted as a single assistant row with both a
    ``ToolCall`` and a ``ToolResult`` attached. Anthropic requires the
    ``tool_result`` to live on a user-role message immediately after the
    ``tool_use``, so the request builder must emit three Anthropic messages
    (assistant tool_use → user tool_result → assistant text) for each such
    row. Regression for the 400 "tool_use ids were found without tool_result
    blocks" error that broke every conversation containing a slash command.
    """
    messages = [
        Message(role=MessageRole.USER, content="/recap 7d"),
        Message(
            role=MessageRole.ASSISTANT,
            content="Here's your recap...",
            tool_calls=[
                ToolCall(
                    tool_call_id="slash-abc123",
                    tool_name="time_logs_recap",
                    arguments={"days": 7},
                )
            ],
            tool_results=[
                ToolResult(
                    tool_call_id="slash-abc123",
                    content="Recap: 7 days...",
                    is_error=False,
                )
            ],
        ),
        Message(role=MessageRole.USER, content="show me more"),
    ]

    built = backend._build_messages(messages)

    # Expected: user → assistant(tool_use) → user(tool_result) → assistant(text) → user
    assert len(built) == 5
    assert built[0] == {"role": "user", "content": "/recap 7d"}

    # Split row 1: assistant with only the tool_use block
    assert built[1]["role"] == "assistant"
    assert len(built[1]["content"]) == 1
    assert built[1]["content"][0]["type"] == "tool_use"
    assert built[1]["content"][0]["id"] == "slash-abc123"
    assert built[1]["content"][0]["name"] == "time_logs_recap"

    # Split row 2: user with the matching tool_result
    assert built[2]["role"] == "user"
    assert len(built[2]["content"]) == 1
    assert built[2]["content"][0]["type"] == "tool_result"
    assert built[2]["content"][0]["tool_use_id"] == "slash-abc123"
    assert built[2]["content"][0]["content"] == "Recap: 7 days..."
    assert "is_error" not in built[2]["content"][0]  # not set when False

    # Split row 3: assistant text for alternation
    assert built[3]["role"] == "assistant"
    assert built[3]["content"] == [{"type": "text", "text": "Here's your recap..."}]

    # The user's follow-up must still come after the split
    assert built[4] == {"role": "user", "content": "show me more"}


def test_build_messages_splits_slash_command_error_row(backend: AnthropicAI) -> None:
    """Errored slash-command rows must propagate is_error and fall back on empty text."""
    messages = [
        Message(role=MessageRole.USER, content="/bad"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",  # tool returned no text
            tool_calls=[
                ToolCall(
                    tool_call_id="slash-deadbeef",
                    tool_name="bad_tool",
                    arguments={},
                )
            ],
            tool_results=[
                ToolResult(
                    tool_call_id="slash-deadbeef",
                    content="boom",
                    is_error=True,
                )
            ],
        ),
    ]

    built = backend._build_messages(messages)

    assert len(built) == 4
    # tool_result carries is_error
    assert built[2]["content"][0]["is_error"] is True
    # Empty content falls back to placeholder so we don't ship an empty array
    assert built[3]["content"][0]["text"] == "(done)"


async def test_generate_raises_ai_backend_error_on_non_json_error(
    backend: AnthropicAI,
) -> None:
    """A 5xx with a non-JSON body should still produce a non-empty error message."""
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 502
    mock_response.json.side_effect = ValueError("not json")
    mock_response.text = "<html>Bad Gateway</html>"

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Test")],
    )

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(request)

    assert exc_info.value.status == 502
    assert "Bad Gateway" in str(exc_info.value)

    await backend.close()


async def test_generate_raises_when_not_initialized(backend: AnthropicAI) -> None:
    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(request)


# --- Dangling tool_use heal ---


def test_dangling_tool_use_merged_into_next_user_message() -> None:
    """An assistant tool_use with no tool_result and a following plain
    user message must get synthetic tool_result blocks folded in."""
    backend = AnthropicAI()
    messages = [
        Message(role=MessageRole.USER, content="make a PO"),
        Message(
            role=MessageRole.ASSISTANT,
            content="I'll build it",
            tool_calls=[
                ToolCall(
                    tool_call_id="toolu_orphan",
                    tool_name="upload_document",
                    arguments={"source": "local"},
                )
            ],
        ),
        # ← note: no TOOL_RESULT row. This is the pre-fix persisted shape.
        Message(role=MessageRole.USER, content="actually, never mind"),
    ]
    result = backend._build_messages(messages)

    # Expected: user / assistant / user — the dangling tool_use got paired
    # via synthetic tool_result folded into the second user turn.
    assert len(result) == 3
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"
    assert result[2]["role"] == "user"

    # The assistant row still has its original tool_use block.
    asst_blocks = result[1]["content"]
    assert any(
        isinstance(b, dict)
        and b.get("type") == "tool_use"
        and b.get("id") == "toolu_orphan"
        for b in asst_blocks
    )

    # The next user message now starts with a synthetic tool_result
    # pairing that id, followed by the user's actual text.
    healed_user = result[2]["content"]
    assert isinstance(healed_user, list)
    assert healed_user[0]["type"] == "tool_result"
    assert healed_user[0]["tool_use_id"] == "toolu_orphan"
    assert healed_user[0].get("is_error") is True
    assert any(
        isinstance(b, dict) and b.get("type") == "text"
        and b.get("text") == "actually, never mind"
        for b in healed_user[1:]
    )


def test_dangling_tool_use_at_end_inserts_synthetic_user_turn() -> None:
    """When the dangling tool_use is the last message and no user turn
    follows, the heal appends a synthetic user turn so the final state
    is a valid request shape."""
    backend = AnthropicAI()
    messages = [
        Message(role=MessageRole.USER, content="make a PO"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(
                    tool_call_id="toolu_trailing",
                    tool_name="run_script",
                    arguments={},
                )
            ],
        ),
    ]
    result = backend._build_messages(messages)

    assert len(result) == 3
    assert result[2]["role"] == "user"
    assert result[2]["content"][0]["type"] == "tool_result"
    assert result[2]["content"][0]["tool_use_id"] == "toolu_trailing"


def test_well_formed_tool_use_tool_result_pair_untouched() -> None:
    """The heal is a no-op on conversations that already pair every
    tool_use with a tool_result — regression guard."""
    backend = AnthropicAI()
    messages = [
        Message(role=MessageRole.USER, content="weather?"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(
                    tool_call_id="toolu_ok",
                    tool_name="get_weather",
                    arguments={"city": "Portland"},
                )
            ],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(
                    tool_call_id="toolu_ok",
                    content='{"temp": 72}',
                )
            ],
        ),
        Message(role=MessageRole.ASSISTANT, content="It's 72."),
    ]
    result = backend._build_messages(messages)
    # user, assistant (tool_use), user (tool_result), assistant (text)
    assert [m["role"] for m in result] == ["user", "assistant", "user", "assistant"]
    # No synthetic injected — the tool_result on the user row is the
    # real one the service produced, content is "{\"temp\": 72}".
    tr_block = result[2]["content"][0]
    assert tr_block["tool_use_id"] == "toolu_ok"
    assert tr_block["content"] == '{"temp": 72}'
    assert "is_error" not in tr_block


# --- Capabilities ---


def test_capabilities_reports_streaming_and_attachments() -> None:
    backend = AnthropicAI()
    caps = backend.capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


# --- Streaming (generate_stream via mocked SSE) ---


class _FakeStreamResponse:
    """Minimal async-context-manager stand-in for httpx streaming response.

    Lets tests feed a canned sequence of SSE lines (as a list[str]) into
    ``generate_stream`` without spinning up a real httpx transport. Fans
    out the lines one per ``aiter_lines`` step. The blank-line event
    delimiters are part of the input so the parser's state machine sees
    the same shape it would off the wire.
    """

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.is_error = status_code >= 400
        self._body_bytes = b""

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def aiter_lines(self):  # type: ignore[no-untyped-def]
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body_bytes


def _sse(event: str, data: dict) -> list[str]:
    import json as _j

    return [f"event: {event}", f"data: {_j.dumps(data)}", ""]


async def test_generate_stream_text_deltas_and_complete(backend: AnthropicAI) -> None:
    """SSE text_delta events become StreamEventType.TEXT_DELTA, and the
    final MESSAGE_COMPLETE carries the assembled AIResponse."""
    from gilbert.interfaces.ai import StreamEventType

    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-6",
                "stop_reason": None,
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        },
    )
    lines += _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello "},
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "world"},
        },
    )
    lines += _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )
    lines += _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
    )
    lines += _sse("message_stop", {"type": "message_stop"})

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    events = []
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    ):
        events.append(ev)

    text_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
    assert [e.text for e in text_events] == ["Hello ", "world"]

    completes = [e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE]
    assert len(completes) == 1
    final = completes[0].response
    assert final is not None
    assert final.message.content == "Hello world"
    assert final.stop_reason == StopReason.END_TURN
    assert final.usage is not None
    assert final.usage.input_tokens == 12
    assert final.usage.output_tokens == 3

    await backend.close()


async def test_generate_stream_tool_use_reassembly(backend: AnthropicAI) -> None:
    """tool_use blocks are assembled from partial_json deltas so the
    final AIResponse carries complete ToolCall arguments."""
    from gilbert.interfaces.ai import StreamEventType

    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-6",
                "stop_reason": None,
                "usage": {"input_tokens": 20, "output_tokens": 0},
            },
        },
    )
    lines += _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "get_weather",
                "input": {},
            },
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city": "Portl'},
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": 'and"}'},
        },
    )
    lines += _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )
    lines += _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 8},
        },
    )
    lines += _sse("message_stop", {"type": "message_stop"})

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    events = []
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="weather?")])
    ):
        events.append(ev)

    # Start/delta/end events for the tool call
    starts = [e for e in events if e.type == StreamEventType.TOOL_CALL_START]
    deltas = [e for e in events if e.type == StreamEventType.TOOL_CALL_DELTA]
    ends = [e for e in events if e.type == StreamEventType.TOOL_CALL_END]
    assert len(starts) == 1
    assert starts[0].tool_call_id == "toolu_01"
    assert starts[0].tool_name == "get_weather"
    assert len(deltas) == 2
    assert len(ends) == 1

    # Final assembled response
    completes = [e for e in events if e.type == StreamEventType.MESSAGE_COMPLETE]
    assert len(completes) == 1
    final = completes[0].response
    assert final is not None
    assert final.stop_reason == StopReason.TOOL_USE
    assert len(final.message.tool_calls) == 1
    tc = final.message.tool_calls[0]
    assert tc.tool_call_id == "toolu_01"
    assert tc.tool_name == "get_weather"
    assert tc.arguments == {"city": "Portland"}

    await backend.close()


async def test_generate_stream_accumulates_cache_tokens(
    backend: AnthropicAI,
) -> None:
    """cache_creation_input_tokens + cache_read_input_tokens from message_start
    carry through to the final AIResponse.usage."""
    from gilbert.interfaces.ai import StreamEventType

    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_cache",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-6",
                "stop_reason": None,
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 1000,
                },
            },
        },
    )
    lines += _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hi"},
        },
    )
    lines += _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )
    lines += _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
    )
    lines += _sse("message_stop", {"type": "message_stop"})

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    final = None
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    ):
        if ev.type == StreamEventType.MESSAGE_COMPLETE:
            final = ev.response

    assert final is not None
    assert final.usage is not None
    assert final.usage.input_tokens == 50
    assert final.usage.output_tokens == 3
    assert final.usage.cache_creation_tokens == 200
    assert final.usage.cache_read_tokens == 1000

    await backend.close()


async def test_generate_stream_max_tokens_stop_reason(backend: AnthropicAI) -> None:
    """A max_tokens SSE stop reason translates to StopReason.MAX_TOKENS."""
    from gilbert.interfaces.ai import StreamEventType

    await backend.initialize({"api_key": "sk-test"})
    assert backend._client is not None

    lines = []
    lines += _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_3",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-6",
                "stop_reason": None,
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
    )
    lines += _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    lines += _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "truncated"},
        },
    )
    lines += _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
            "usage": {"output_tokens": 4096},
        },
    )
    lines += _sse("message_stop", {"type": "message_stop"})

    fake = _FakeStreamResponse(lines)
    backend._client.stream = MagicMock(return_value=fake)  # type: ignore[method-assign]

    final_response = None
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="long answer")])
    ):
        if ev.type == StreamEventType.MESSAGE_COMPLETE:
            final_response = ev.response

    assert final_response is not None
    assert final_response.stop_reason == StopReason.MAX_TOKENS
    assert final_response.message.content == "truncated"

    await backend.close()


# --- Prompt caching ───────────────────────────────────────────────────────
#
# The win we're paying for: Anthropic prompt caching cuts input-token cost
# by ~10x on cached reads. These tests pin where the cache_control markers
# go in the request body so a future refactor can't silently remove them.


def test_build_tools_sorts_by_name_for_cache_stability() -> None:
    """Tools must serialize in a deterministic order — Anthropic's
    cache invalidates if the tools block differs by even one byte
    between calls. Python's dict iteration is technically insertion-
    ordered, but discovery upstream may reorder under concurrency.
    Sorting by name removes the entire class of race."""
    tools = [
        ToolDefinition(name="zebra", description="z"),
        ToolDefinition(name="apple", description="a"),
        ToolDefinition(name="mango", description="m"),
    ]
    result = AnthropicAI._build_tools(tools)
    assert [t["name"] for t in result] == ["apple", "mango", "zebra"]


def test_build_tools_marks_last_tool_cacheable() -> None:
    """The LAST tool gets ``cache_control: ephemeral``. Anthropic
    caches the prefix up to and including that marker — with ~190
    tools at ~15-25k tokens combined, this is the single biggest
    cache win in the request."""
    tools = [
        ToolDefinition(name="a", description="a"),
        ToolDefinition(name="b", description="b"),
        ToolDefinition(name="c", description="c"),
    ]
    result = AnthropicAI._build_tools(tools)
    # Only the last (sorted: "c") has the marker.
    assert "cache_control" not in result[0]
    assert "cache_control" not in result[1]
    assert result[-1]["cache_control"] == {"type": "ephemeral"}


def test_build_tools_empty_list_does_not_crash() -> None:
    """Edge case — agents that disable tools entirely. Empty list
    must round-trip without an IndexError trying to mark a [-1]
    that doesn't exist."""
    assert AnthropicAI._build_tools([]) == []


def test_system_prompt_is_a_content_block_list() -> None:
    """System prompt arrives as a list of content blocks (not a
    bare string) so the cache_control marker can attach. Anthropic
    accepts both shapes; only the list form is cacheable."""
    backend = AnthropicAI()
    backend._model = "m"
    backend._max_tokens = 100
    backend._temperature = 0.3
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="hi")],
        system_prompt="You are Gilbert.",
    )
    body = backend._build_request_body(request)
    sys = body["system"]
    assert isinstance(sys, list)
    assert sys[0]["type"] == "text"
    assert sys[0]["text"] == "You are Gilbert."
    assert sys[0]["cache_control"] == {"type": "ephemeral"}


def test_last_user_message_gets_cache_marker() -> None:
    """The cache marker on the last message extends the cached
    prefix to cover the entire conversation history. Next call within
    5 minutes reads system + tools + history at the 0.1× cache rate;
    only the new turn after this marker is fresh input."""
    backend = AnthropicAI()
    backend._model = "m"
    backend._max_tokens = 100
    backend._temperature = 0.3
    request = AIRequest(
        messages=[
            Message(role=MessageRole.USER, content="What's the weather?"),
            Message(role=MessageRole.ASSISTANT, content="Let me check..."),
            Message(role=MessageRole.USER, content="Today specifically."),
        ],
    )
    body = backend._build_request_body(request)
    last_msg = body["messages"][-1]
    content = last_msg["content"]
    # String content gets promoted to a single text block carrying
    # the marker.
    assert isinstance(content, list)
    assert content[-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier messages stay un-tagged — Anthropic only allows 4
    # markers total and we're not wasting any on prior turns.
    earlier = body["messages"][:-1]
    for m in earlier:
        c = m.get("content")
        if isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    assert "cache_control" not in block


def test_last_message_with_list_content_tags_last_block() -> None:
    """When the last message is already a list of content blocks
    (multimodal turn — text + image), the marker attaches to the
    LAST block. Anthropic caches the prefix up to and including the
    marker, so trailing position is what we want."""
    backend = AnthropicAI()
    backend._model = "m"
    backend._max_tokens = 100
    backend._temperature = 0.3
    request = AIRequest(
        messages=[
            Message(
                role=MessageRole.USER,
                content="What's in this image?",
                attachments=[],  # text-only, but shapes the list form
            ),
        ],
    )
    body = backend._build_request_body(request)
    last = body["messages"][-1]
    content = last["content"]
    assert isinstance(content, list)
    assert content[-1].get("cache_control") == {"type": "ephemeral"}
