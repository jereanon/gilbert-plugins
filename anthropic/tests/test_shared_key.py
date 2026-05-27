"""Tests for the plugin-local Anthropic API key registry.

The shared registry lets sibling backends (AI / Vision) reuse one
operator-supplied key instead of forcing duplicate entry under
Settings → AI and Settings → Vision. Covered:
- get returns "" when nothing's been registered
- register stores non-empty values, ignores empty
- vision's initialize() picks up a key seeded by AI's initialize
- per-backend config still wins when both are set
"""

from __future__ import annotations

import pytest


def setup_function(_fn) -> None:
    """Reset the registry before each test so test order doesn't
    leak state."""
    from gilbert_plugin_anthropic.shared_key import _reset_for_testing

    _reset_for_testing()


def test_get_returns_empty_when_nothing_registered() -> None:
    from gilbert_plugin_anthropic.shared_key import (
        get_shared_anthropic_api_key,
    )

    assert get_shared_anthropic_api_key() == ""


def test_register_stores_non_empty_key() -> None:
    from gilbert_plugin_anthropic.shared_key import (
        get_shared_anthropic_api_key,
        register_anthropic_api_key,
    )

    register_anthropic_api_key("sk-ant-fake-123", source="ai")
    assert get_shared_anthropic_api_key() == "sk-ant-fake-123"


def test_register_ignores_empty_string() -> None:
    """A backend with no configured key calls register() with "" —
    that should NOT clobber an existing shared key (otherwise
    backend startup order would matter)."""
    from gilbert_plugin_anthropic.shared_key import (
        get_shared_anthropic_api_key,
        register_anthropic_api_key,
    )

    register_anthropic_api_key("sk-ant-real", source="ai")
    register_anthropic_api_key("", source="vision")
    assert get_shared_anthropic_api_key() == "sk-ant-real"


def test_register_updates_when_value_changes() -> None:
    """If a backend explicitly sets a different key (operator
    rotated under Settings → Vision), that supersedes the prior
    shared value. The most-recently-seen non-empty wins."""
    from gilbert_plugin_anthropic.shared_key import (
        get_shared_anthropic_api_key,
        register_anthropic_api_key,
    )

    register_anthropic_api_key("sk-ant-old", source="ai")
    register_anthropic_api_key("sk-ant-new", source="vision")
    assert get_shared_anthropic_api_key() == "sk-ant-new"


# ── Vision integration ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_uses_shared_key_when_own_config_empty() -> None:
    """The headline behavior: AI starts with a key, registers it.
    Vision starts WITHOUT a key in its own config but inherits the
    shared one and becomes available."""
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision
    from gilbert_plugin_anthropic.shared_key import (
        register_anthropic_api_key,
    )

    # Simulate AnthropicAI.initialize having already registered.
    register_anthropic_api_key("sk-ant-from-ai", source="ai")

    vision = AnthropicVision()
    await vision.initialize({"api_key": "", "model": "claude-vision-x"})

    assert vision._api_key == "sk-ant-from-ai"
    assert vision.available is True


@pytest.mark.asyncio
async def test_vision_own_config_wins_over_shared_key() -> None:
    """When the operator explicitly sets a different key under
    Settings → Vision, that wins — useful for multi-tenant deploys
    where vision should hit a different account / budget."""
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision
    from gilbert_plugin_anthropic.shared_key import (
        register_anthropic_api_key,
    )

    register_anthropic_api_key("sk-ant-from-ai", source="ai")

    vision = AnthropicVision()
    await vision.initialize(
        {"api_key": "sk-ant-vision-explicit", "model": "claude-vision-x"}
    )

    assert vision._api_key == "sk-ant-vision-explicit"


@pytest.mark.asyncio
async def test_vision_no_keys_anywhere_stays_unavailable() -> None:
    """No key in vision's own config AND nothing in the registry →
    vision reports unavailable (existing behavior, just shouldn't
    regress)."""
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision

    vision = AnthropicVision()
    await vision.initialize({"api_key": ""})

    assert vision._api_key == ""
    assert vision.available is False


@pytest.mark.asyncio
async def test_vision_registers_its_own_key_too() -> None:
    """When vision DOES have its own key, it registers it so
    siblings (today none, tomorrow Anthropic OCR / etc.) can
    benefit if THEY didn't get one."""
    from gilbert_plugin_anthropic.anthropic_vision import AnthropicVision
    from gilbert_plugin_anthropic.shared_key import (
        get_shared_anthropic_api_key,
    )

    vision = AnthropicVision()
    await vision.initialize({"api_key": "sk-ant-vision-first"})

    assert get_shared_anthropic_api_key() == "sk-ant-vision-first"
