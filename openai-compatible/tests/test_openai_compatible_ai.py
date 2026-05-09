"""Tests for OpenAICompatibleAI backend — vendor-neutral Chat Completions."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_openai_compatible.openai_compatible_ai import (
    OpenAICompatibleAI,
    _parse_header_lines,
)

from gilbert.interfaces.ai import (
    AIBackendError,
    AIRequest,
    FileAttachment,
    Message,
    MessageRole,
    StopReason,
    StreamEventType,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)


@pytest.fixture
def backend() -> OpenAICompatibleAI:
    return OpenAICompatibleAI()


# --- Initialization ---


async def test_initialize_requires_base_url(backend: OpenAICompatibleAI) -> None:
    with pytest.raises(ValueError, match="base_url"):
        await backend.initialize({})


async def test_initialize_api_key_optional(backend: OpenAICompatibleAI) -> None:
    """Local proxies (Ollama, LM Studio) don't need an API key —
    initialize must succeed without one and not send an Authorization
    header."""
    await backend.initialize({"base_url": "http://localhost:11434/v1"})
    assert backend._client is not None
    assert "Authorization" not in backend._client.headers
    await backend.close()


async def test_initialize_sends_bearer_when_api_key_set(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize({"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk_x"})
    assert backend._client is not None
    assert backend._client.headers.get("Authorization") == "Bearer gsk_x"
    await backend.close()


async def test_initialize_merges_custom_request_headers(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize(
        {
            "base_url": "https://proxy.example/v1",
            "request_headers": "x-api-key: abc\n# comment\nx-workspace: team1",
        }
    )
    assert backend._client is not None
    assert backend._client.headers.get("x-api-key") == "abc"
    assert backend._client.headers.get("x-workspace") == "team1"
    await backend.close()


async def test_initialize_strips_trailing_slash_on_base_url(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize({"base_url": "https://proxy.example/v1/"})
    assert backend._client is not None
    resolved = str(backend._client.build_request("POST", "/chat/completions").url)
    assert resolved == "https://proxy.example/v1/chat/completions"
    await backend.close()


async def test_close_clears_client(backend: OpenAICompatibleAI) -> None:
    await backend.initialize({"base_url": "http://localhost:1234"})
    await backend.close()
    assert backend._client is None


def test_parse_header_lines_ignores_comments_and_blank_lines() -> None:
    assert _parse_header_lines("\n# leading comment\nfoo: bar\n\nbaz : qux\n") == {
        "foo": "bar",
        "baz": "qux",
    }


def test_parse_header_lines_skips_malformed() -> None:
    assert _parse_header_lines("ok: yes\nno-colon-here\nanother: value") == {
        "ok": "yes",
        "another": "value",
    }


# --- Capabilities & model discovery ---


def test_capabilities_reports_streaming_and_attachments() -> None:
    caps = OpenAICompatibleAI().capabilities()
    assert caps.streaming is True
    assert caps.attachments_user is True


def test_capabilities_disables_streaming_when_unsupported() -> None:
    backend = OpenAICompatibleAI()
    backend._supports_streaming = False
    assert backend.capabilities().streaming is False


def test_available_models_empty_by_default() -> None:
    """Unlike the first-party OpenAI plugin, there is no hardcoded
    catalog — the chat UI falls back to the free-form ``model`` field."""
    assert OpenAICompatibleAI().available_models() == []


def test_available_models_after_refresh() -> None:
    backend = OpenAICompatibleAI()
    from gilbert.interfaces.ai import ModelInfo

    backend._discovered_models = [ModelInfo(id="llama-3.1-70b", name="llama-3.1-70b")]
    models = backend.available_models()
    assert [m.id for m in models] == ["llama-3.1-70b"]


# --- Request building ---


def test_build_request_body_uses_max_tokens_not_max_completion_tokens() -> None:
    """The OpenAI-specific ``max_completion_tokens`` rename only matters
    for OpenAI's o-series models. Every other OpenAI-compatible
    endpoint accepts the original ``max_tokens`` field."""
    backend = OpenAICompatibleAI()
    backend._model = "llama-3.1-70b"
    backend._max_tokens = 2048
    backend._temperature = 0.5
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
    )
    body = backend._build_request_body(request)
    assert body["max_tokens"] == 2048
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.5


def test_build_request_body_always_sends_temperature() -> None:
    """No o-series special casing — temperature always rides."""
    backend = OpenAICompatibleAI()
    backend._model = "o1-mini"  # name looks OpenAI-y but we don't care
    request = AIRequest(messages=[Message(role=MessageRole.USER, content="Hi")])
    body = backend._build_request_body(request)
    assert "temperature" in body


def test_build_request_body_raises_when_no_model() -> None:
    backend = OpenAICompatibleAI()
    with pytest.raises(AIBackendError, match="model"):
        backend._build_request_body(
            AIRequest(messages=[Message(role=MessageRole.USER, content="Hi")])
        )


def test_build_request_body_per_request_model_override() -> None:
    backend = OpenAICompatibleAI()
    backend._model = "default-model"
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        model="llama-3.1-70b",
    )
    body = backend._build_request_body(request)
    assert body["model"] == "llama-3.1-70b"


def test_build_request_body_rejects_tools_when_disabled() -> None:
    backend = OpenAICompatibleAI()
    backend._model = "llama-3.1-70b"
    backend._supports_tools = False
    request = AIRequest(
        messages=[Message(role=MessageRole.USER, content="Hi")],
        tools=[
            ToolDefinition(
                name="search",
                description="Search",
                parameters=[
                    ToolParameter(name="q", type=ToolParameterType.STRING, description="query")
                ],
            )
        ],
    )
    with pytest.raises(AIBackendError, match="supports_tools"):
        backend._build_request_body(request)


def test_build_messages_system_prompt_prepended() -> None:
    backend = OpenAICompatibleAI()
    messages = [Message(role=MessageRole.USER, content="Hi")]
    result = backend._build_messages(messages, "Be helpful")
    assert result[0] == {"role": "system", "content": "Be helpful"}
    assert result[1] == {"role": "user", "content": "Hi"}


def test_build_messages_image_attachment_becomes_image_url_part() -> None:
    backend = OpenAICompatibleAI()
    messages = [
        Message(
            role=MessageRole.USER,
            content="what is this?",
            attachments=[
                FileAttachment(kind="image", media_type="image/png", data="AAAA"),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    content = result[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_build_messages_assistant_tool_calls_encoded_as_json_string() -> None:
    backend = OpenAICompatibleAI()
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
    result = backend._build_messages(messages, "")
    assert result[0]["role"] == "assistant"
    tcs = result[0]["tool_calls"]
    assert tcs[0]["function"]["arguments"] == '{"q": "test"}'


def test_build_messages_tool_result_row() -> None:
    backend = OpenAICompatibleAI()
    messages = [
        Message(
            role=MessageRole.TOOL_RESULT,
            tool_results=[
                ToolResult(tool_call_id="tc_1", content="found it"),
            ],
        )
    ]
    result = backend._build_messages(messages, "")
    assert result == [
        {"role": "tool", "tool_call_id": "tc_1", "content": "found it"},
    ]


# --- Response parsing ---


def test_parse_text_response() -> None:
    backend = OpenAICompatibleAI()
    backend._model = "llama-3.1-70b"
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama-3.1-70b",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    response = backend._parse_response(data)
    assert response.message.content == "Hello!"
    assert response.model == "llama-3.1-70b"
    assert response.stop_reason == StopReason.END_TURN
    assert response.usage is not None
    assert response.usage.input_tokens == 10


def test_parse_tool_use_response() -> None:
    backend = OpenAICompatibleAI()
    backend._model = "llama-3.1-70b"
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"q": "weather"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "model": "llama-3.1-70b",
    }
    response = backend._parse_response(data)
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.message.tool_calls[0].arguments == {"q": "weather"}


# --- generate (mocked HTTP) ---


async def test_generate_calls_chat_completions(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize(
        {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-70b"}
    )

    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "API response"},
                "finish_reason": "stop",
            }
        ],
        "model": "llama-3.1-70b",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    response = await backend.generate(
        AIRequest(messages=[Message(role=MessageRole.USER, content="Test")])
    )
    assert response.message.content == "API response"
    assert backend._client.post.call_args[0][0] == "/chat/completions"

    await backend.close()


async def test_generate_raises_ai_backend_error_on_http_error(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize(
        {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-70b"}
    )

    mock_response = MagicMock()
    mock_response.is_error = True
    mock_response.status_code = 400
    mock_response.json.return_value = {"error": {"message": "Invalid model: not-a-model"}}

    assert backend._client is not None
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    with pytest.raises(AIBackendError) as exc_info:
        await backend.generate(AIRequest(messages=[Message(role=MessageRole.USER, content="Test")]))
    assert exc_info.value.status == 400
    assert "Invalid model: not-a-model" in str(exc_info.value)

    await backend.close()


async def test_generate_raises_when_not_initialized(
    backend: OpenAICompatibleAI,
) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await backend.generate(AIRequest(messages=[Message(role=MessageRole.USER, content="Test")]))


# --- refresh_models action ---


async def test_refresh_models_populates_discovered_list(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize({"base_url": "https://api.groq.com/openai/v1"})

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.is_error = False
    mock_response.json.return_value = {
        "data": [
            {"id": "llama-3.1-70b", "object": "model"},
            {"id": "mixtral-8x7b", "object": "model"},
        ]
    }

    assert backend._client is not None
    backend._client.get = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await backend._action_refresh_models()
    assert result.status == "ok"
    assert sorted(m.id for m in backend._discovered_models) == [
        "llama-3.1-70b",
        "mixtral-8x7b",
    ]
    assert backend._client.get.call_args[0][0] == "/models"

    await backend.close()


async def test_refresh_models_reports_404(backend: OpenAICompatibleAI) -> None:
    """When the endpoint doesn't implement ``/models``, tell the user
    to type the model ID manually — don't silently accept an empty
    list."""
    await backend.initialize({"base_url": "http://localhost:8080/v1"})

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.is_error = True

    assert backend._client is not None
    backend._client.get = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    result = await backend._action_refresh_models()
    assert result.status == "error"
    assert "doesn't implement" in result.message

    await backend.close()


# --- Streaming ---


class _FakeStreamResponse:
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


def _sse_chunk(payload: dict) -> list[str]:
    import json as _j

    return [f"data: {_j.dumps(payload)}", ""]


async def test_generate_stream_text_deltas_and_complete(
    backend: OpenAICompatibleAI,
) -> None:
    await backend.initialize(
        {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-70b"}
    )
    assert backend._client is not None

    lines: list[str] = []
    lines += _sse_chunk(
        {
            "id": "c1",
            "model": "llama-3.1-70b",
            "choices": [{"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}],
        }
    )
    lines += _sse_chunk(
        {
            "id": "c1",
            "model": "llama-3.1-70b",
            "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": None}],
        }
    )
    lines += _sse_chunk(
        {
            "id": "c1",
            "model": "llama-3.1-70b",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
    )
    lines.append("data: [DONE]")
    lines.append("")

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

    await backend.close()


async def test_generate_stream_falls_back_to_non_streaming(
    backend: OpenAICompatibleAI,
) -> None:
    """With ``supports_streaming=false`` the stream path must route
    through a single non-streaming request, yielding one MESSAGE_COMPLETE."""
    await backend.initialize(
        {
            "base_url": "http://localhost:8080/v1",
            "model": "local-model",
            "supports_streaming": False,
        }
    )
    assert backend._client is not None

    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "non-streamed"},
                "finish_reason": "stop",
            }
        ],
        "model": "local-model",
    }
    backend._client.post = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

    events = []
    async for ev in backend.generate_stream(
        AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
    ):
        events.append(ev)

    # No text deltas — just the single MESSAGE_COMPLETE.
    assert [e.type for e in events] == [StreamEventType.MESSAGE_COMPLETE]
    final = events[0].response
    assert final is not None
    assert final.message.content == "non-streamed"

    await backend.close()


async def test_generate_stream_raises_on_http_error(
    backend: OpenAICompatibleAI,
) -> None:
    import json as _j

    await backend.initialize(
        {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-70b"}
    )
    assert backend._client is not None

    class _ErrStream(_FakeStreamResponse):
        def __init__(self) -> None:
            super().__init__(lines=[], status_code=401)
            self._body_bytes = _j.dumps({"error": {"message": "Invalid API key"}}).encode()

    backend._client.stream = MagicMock(return_value=_ErrStream())  # type: ignore[method-assign]

    with pytest.raises(AIBackendError) as exc_info:
        async for _ev in backend.generate_stream(
            AIRequest(messages=[Message(role=MessageRole.USER, content="hi")])
        ):
            pass

    assert exc_info.value.status == 401
    assert "Invalid API key" in str(exc_info.value)

    await backend.close()
