"""Tests for AnthropicVision's prompt selection.

The earlier hardcoded prompt told Claude to "respond with an empty
string if the page contains no technical content" — written for PDF
knowledge indexing but used as the default for every caller. The
Mentra camera_tool inherited it and got back empty descriptions for
every general photo, breaking the "what am I looking at?" flow
entirely.

These tests pin the contract:

- The bundled default prompt is general-purpose (does NOT instruct
  the model to return empty under any condition).
- A caller's ``prompt=`` argument wins over the backend default.
- An empty / whitespace-only caller prompt falls back to the
  backend default (so passing prompt="" doesn't accidentally erase
  whatever the operator configured).
- Operator-configured prompt in ``initialize(config)`` becomes the
  default (used when callers don't pass their own).

We don't make real Anthropic calls — we stub the SDK client and
assert what got passed in the messages array.
"""

from __future__ import annotations

import pytest


def _make_vision_with_stub_client() -> tuple:
    """Build an AnthropicVision whose SDK client is replaced with a
    recorder so tests don't make network calls and don't need the
    ``anthropic`` package installed. Returns ``(vision, captured)``
    where ``captured`` is a dict the stub fills in on each
    ``messages.create`` call."""
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision

    vision = AnthropicVision()
    captured: dict[str, object] = {}

    class _StubMessages:
        @staticmethod
        def create(*, model, max_tokens, messages):
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["messages"] = messages

            class _Block:
                text = "stub response"

            class _Resp:
                content = [_Block()]

            return _Resp()

    class _StubClient:
        messages = _StubMessages

    # Override ``_get_client`` entirely so it always returns our stub
    # — bypasses the key-rotation check that would otherwise try to
    # ``import anthropic`` (not necessarily installed in the test env).
    vision._get_client = lambda: _StubClient()  # type: ignore[method-assign]
    return vision, captured


@pytest.mark.asyncio
async def test_default_prompt_does_not_instruct_empty_response() -> None:
    """Regression guard. The earlier default said "respond with an
    empty string if no technical content" — that broke the camera
    tool for every non-technical photo. The new default must NOT
    contain that instruction under any phrasing."""
    from gilbert_plugin_anthropic.anthropic_vision import _DEFAULT_PROMPT

    lower = _DEFAULT_PROMPT.lower()
    # Forbidden phrasings — these were the smoking gun.
    assert "respond with an empty string" not in lower
    assert "return an empty string" not in lower
    # Required: explicit "always return something".
    assert "never return an empty response" in lower


@pytest.mark.asyncio
async def test_caller_prompt_wins_over_backend_default() -> None:
    """A caller (e.g. Mentra camera_tool) that knows what kind of
    description it wants passes ``prompt=`` and that text must be
    what Claude receives — NOT the backend's operator-tuned default."""
    vision, captured = _make_vision_with_stub_client()
    await vision.initialize({"api_key": "sk-ant-fake", "prompt": ""})

    caller_prompt = "Describe any people in plain language. Don't speculate on identity."
    result = await vision.describe_image(
        b"\xff\xd8\xff\xe0fakejpegbytes",
        "image/jpeg",
        prompt=caller_prompt,
    )

    assert result == "stub response"
    # Inspect the messages array sent to Claude — the prompt text
    # block should match the caller's prompt verbatim.
    msgs = captured["messages"]
    text_blocks = [b for b in msgs[0]["content"] if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"] == caller_prompt


@pytest.mark.asyncio
async def test_empty_caller_prompt_falls_back_to_backend_default() -> None:
    """``describe_image(..., prompt="")`` means "no opinion" — use
    whatever the backend was configured with. Tests the inverse of
    the override path: existing callers (knowledge indexing, etc.)
    that pass non-empty prompts aren't affected, but a caller that
    omits the kwarg gets the operator-tuned default rather than
    silently sending no instructions at all."""
    from gilbert_plugin_anthropic.anthropic_vision import _DEFAULT_PROMPT

    vision, captured = _make_vision_with_stub_client()
    await vision.initialize({"api_key": "sk-ant-fake"})

    await vision.describe_image(b"bytes", "image/jpeg")
    msgs = captured["messages"]
    text_blocks = [b for b in msgs[0]["content"] if b.get("type") == "text"]
    assert text_blocks[0]["text"] == _DEFAULT_PROMPT


@pytest.mark.asyncio
async def test_whitespace_caller_prompt_falls_back_to_default() -> None:
    """Whitespace-only is treated as empty. Guards against an
    operator pasting a blank textarea and silently losing the
    bundled default."""
    from gilbert_plugin_anthropic.anthropic_vision import _DEFAULT_PROMPT

    vision, captured = _make_vision_with_stub_client()
    await vision.initialize({"api_key": "sk-ant-fake"})

    await vision.describe_image(b"bytes", "image/jpeg", prompt="   \n  \t  ")
    msgs = captured["messages"]
    text_blocks = [b for b in msgs[0]["content"] if b.get("type") == "text"]
    assert text_blocks[0]["text"] == _DEFAULT_PROMPT


@pytest.mark.asyncio
async def test_operator_configured_prompt_becomes_default() -> None:
    """When the operator sets a custom prompt under Settings → Vision
    → Prompt, that's the new fallback for callers that don't pass
    their own. Lets per-deployment tuning work without every caller
    having to opt in."""
    vision, captured = _make_vision_with_stub_client()
    custom_default = "Look like a pirate would. Arrr."
    await vision.initialize(
        {"api_key": "sk-ant-fake", "prompt": custom_default}
    )

    await vision.describe_image(b"bytes", "image/jpeg")
    msgs = captured["messages"]
    text_blocks = [b for b in msgs[0]["content"] if b.get("type") == "text"]
    assert text_blocks[0]["text"] == custom_default


@pytest.mark.asyncio
async def test_caller_prompt_still_wins_over_operator_default() -> None:
    """Even when the operator has tuned the default, a caller's
    explicit prompt overrides — knowledge indexing's technical-
    extraction prompt and the camera_tool's scene prompt must NOT
    be silently replaced by whatever the operator set."""
    vision, captured = _make_vision_with_stub_client()
    await vision.initialize(
        {"api_key": "sk-ant-fake", "prompt": "operator default"}
    )

    await vision.describe_image(b"bytes", "image/jpeg", prompt="caller wins")
    msgs = captured["messages"]
    text_blocks = [b for b in msgs[0]["content"] if b.get("type") == "text"]
    assert text_blocks[0]["text"] == "caller wins"


def test_prompt_configparam_is_exposed_as_ai_prompt() -> None:
    """The prompt MUST be exposed as a ConfigParam(ai_prompt=True)
    so the Settings UI renders the AI-prompt-aware textarea (longer
    rows, syntax-aware) instead of a one-line string field. Required
    by the architecture rule: every non-trivial AI prompt is a
    ConfigParam(ai_prompt=True) on the owning service.
    """
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision

    params = AnthropicVision.backend_config_params()
    prompt_param = next((p for p in params if p.key == "prompt"), None)
    assert prompt_param is not None, "AnthropicVision must expose a 'prompt' ConfigParam"
    assert prompt_param.ai_prompt is True
    assert prompt_param.multiline is True
    assert prompt_param.default  # non-empty bundled default
