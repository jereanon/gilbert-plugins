"""Tests for ElevenLabs TTS backend."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gilbert_plugin_elevenlabs.elevenlabs_tts import ElevenLabsTTS

from gilbert.interfaces.tts import AudioFormat, SynthesisRequest


@pytest.fixture
def backend() -> ElevenLabsTTS:
    return ElevenLabsTTS()


# --- Initialization ---


async def test_initialize_sets_api_key(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._api_key == "sk-test"
    assert backend._client is not None
    await backend.close()


async def test_initialize_default_model(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    assert backend._model_id == "eleven_turbo_v2_5"
    await backend.close()


async def test_initialize_custom_model(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test", "model_id": "eleven_multilingual_v2"})
    assert backend._model_id == "eleven_multilingual_v2"
    await backend.close()


async def test_initialize_requires_api_key(backend: ElevenLabsTTS) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({})


async def test_initialize_rejects_empty_api_key(backend: ElevenLabsTTS) -> None:
    with pytest.raises(ValueError, match="api_key"):
        await backend.initialize({"api_key": ""})


# --- Close ---


async def test_close_clears_client(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})
    await backend.close()
    assert backend._client is None


async def test_close_idempotent(backend: ElevenLabsTTS) -> None:
    await backend.close()  # no-op when not initialized


# --- Client guard ---


def test_require_client_raises_before_init(backend: ElevenLabsTTS) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        backend._require_client()


# --- Synthesize ---


async def test_synthesize_calls_api(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test", "silence_padding": 0})

    mock_response = AsyncMock()
    mock_response.content = b"audio-bytes"
    mock_response.raise_for_status = lambda: None

    with patch.object(backend._client, "post", return_value=mock_response) as mock_post:  # type: ignore[union-attr]
        request = SynthesisRequest(text="Hello", voice_id="voice123")
        result = await backend.synthesize(request)

        mock_post.assert_called_once()
        call_args = mock_post.call_args

        assert "/text-to-speech/voice123" in call_args.args[0]
        assert call_args.kwargs["json"]["text"] == "Hello"
        assert call_args.kwargs["json"]["model_id"] == "eleven_turbo_v2_5"
        assert call_args.kwargs["params"]["output_format"] == "mp3_44100_128"

    assert result.audio == b"audio-bytes"
    assert result.format == AudioFormat.MP3
    assert result.characters_used == 5
    await backend.close()


async def test_synthesize_passes_voice_settings(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = AsyncMock()
    mock_response.content = b"audio"
    mock_response.raise_for_status = lambda: None

    with patch.object(backend._client, "post", return_value=mock_response) as mock_post:  # type: ignore[union-attr]
        request = SynthesisRequest(
            text="Hi",
            voice_id="v1",
            stability=0.7,
            similarity_boost=0.9,
        )
        await backend.synthesize(request)

        body = mock_post.call_args.kwargs["json"]
        assert body["voice_settings"]["stability"] == 0.7
        assert body["voice_settings"]["similarity_boost"] == 0.9

    await backend.close()


# --- List voices ---


async def test_list_voices_parses_response(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "voices": [
            {
                "voice_id": "abc",
                "name": "Rachel",
                "description": "Calm voice",
                "labels": {"accent": "american"},
                "fine_tuning": {"language": "en"},
            },
            {
                "voice_id": "def",
                "name": "Domi",
                "labels": {},
            },
        ]
    }

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voices = await backend.list_voices()

    assert len(voices) == 2
    assert voices[0].voice_id == "abc"
    assert voices[0].name == "Rachel"
    assert voices[0].language == "en"
    assert voices[0].labels == {"accent": "american"}
    assert voices[1].voice_id == "def"
    assert voices[1].language is None
    await backend.close()


# --- Get voice ---


async def test_get_voice_found(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "voice_id": "abc",
        "name": "Rachel",
        "labels": {},
    }

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voice = await backend.get_voice("abc")

    assert voice is not None
    assert voice.voice_id == "abc"
    await backend.close()


async def test_get_voice_not_found(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch.object(backend._client, "get", return_value=mock_response):  # type: ignore[union-attr]
        voice = await backend.get_voice("nonexistent")

    assert voice is None
    await backend.close()


# --- Synthesis cache ---


def _make_mock_response(content: bytes = b"audio-bytes") -> AsyncMock:
    r = AsyncMock()
    r.content = content
    r.raise_for_status = lambda: None
    return r


async def test_cache_hit_skips_api_call(backend: ElevenLabsTTS) -> None:
    """A second identical request is served from the cache."""
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(b"cached-audio"),
    ) as mock_post:
        request = SynthesisRequest(text="Hello", voice_id="v1")
        first = await backend.synthesize(request)
        second = await backend.synthesize(request)

        assert mock_post.call_count == 1
        assert first.audio == b"cached-audio"
        assert second.audio == b"cached-audio"

    stats = backend.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 1
    await backend.close()


async def test_cache_keys_differ_by_text(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="One", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Two", voice_id="v1"))
        assert mock_post.call_count == 2

    assert backend.cache_stats()["size"] == 2
    await backend.close()


async def test_cache_keys_differ_by_voice(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v2"))
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_keys_differ_by_format(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(
                text="Hi",
                voice_id="v1",
                output_format=AudioFormat.MP3,
            )
        )
        await backend.synthesize(
            SynthesisRequest(
                text="Hi",
                voice_id="v1",
                output_format=AudioFormat.WAV,
            )
        )
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_keys_differ_by_voice_settings(
    backend: ElevenLabsTTS,
) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1", stability=0.5))
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1", stability=0.9))
        assert mock_post.call_count == 2

    await backend.close()


async def test_cache_lru_eviction(backend: ElevenLabsTTS) -> None:
    """Inserting past cache_max_entries evicts the least-recently-used."""
    await backend.initialize({"api_key": "sk-test", "cache_max_entries": 2})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="one", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="two", voice_id="v1"))
        # Touch "one" so it's most-recently-used
        await backend.synthesize(SynthesisRequest(text="one", voice_id="v1"))
        # Inserting a third entry evicts the LRU ("two")
        await backend.synthesize(SynthesisRequest(text="three", voice_id="v1"))

    stats = backend.cache_stats()
    assert stats["size"] == 2
    assert stats["evictions"] >= 1
    # "two" should be gone — synthesizing it again triggers a fresh miss
    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="two", voice_id="v1"))
        assert mock_post.call_count == 1

    await backend.close()


async def test_cache_disabled_by_zero_max_entries(
    backend: ElevenLabsTTS,
) -> None:
    await backend.initialize({"api_key": "sk-test", "cache_max_entries": 0})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))
        assert mock_post.call_count == 2

    assert backend.cache_stats()["size"] == 0
    await backend.close()


async def test_cache_ttl_expires_old_entries(backend: ElevenLabsTTS) -> None:
    """Entries older than ttl_seconds are evicted on access."""
    import time as time_mod

    await backend.initialize({"api_key": "sk-test", "cache_ttl_seconds": 0.05})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        request = SynthesisRequest(text="Hi", voice_id="v1")
        await backend.synthesize(request)
        # Wait past the TTL
        time_mod.sleep(0.1)
        await backend.synthesize(request)
        # Second call was NOT a cache hit — the entry expired
        assert mock_post.call_count == 2

    stats = backend.cache_stats()
    assert stats["evictions"] >= 1
    assert stats["misses"] == 2
    await backend.close()


async def test_cache_ttl_zero_disables_expiry(backend: ElevenLabsTTS) -> None:
    """ttl=0 means entries live until LRU evicts them."""
    await backend.initialize({"api_key": "sk-test", "cache_ttl_seconds": 0})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        request = SynthesisRequest(text="Hi", voice_id="v1")
        await backend.synthesize(request)
        await backend.synthesize(request)
        assert mock_post.call_count == 1

    assert backend.cache_stats()["hits"] == 1
    await backend.close()


async def test_close_clears_cache(backend: ElevenLabsTTS) -> None:
    await backend.initialize({"api_key": "sk-test"})

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="Hi", voice_id="v1"))

    assert backend.cache_stats()["size"] == 1
    await backend.close()
    assert backend.cache_stats()["size"] == 0


async def test_config_cache_defaults() -> None:
    """Missing cache config keys fall back to the documented defaults."""
    backend = ElevenLabsTTS()
    await backend.initialize({"api_key": "sk-test"})
    stats = backend.cache_stats()
    assert stats["max_entries"] == 256
    assert stats["ttl_seconds"] == 1800.0
    await backend.close()


# --- Audio-tag injection ---


def _stub_ai(content: str) -> AsyncMock:
    """Build a fake AISamplingProvider whose complete_one_shot returns
    ``content`` as the assistant message text."""
    from gilbert.interfaces.ai import AIResponse, Message, MessageRole, StopReason

    ai = AsyncMock()
    ai.complete_one_shot = AsyncMock(
        return_value=AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=content),
            model="claude-haiku-4-5-20251001",
            stop_reason=StopReason.END_TURN,
        )
    )
    # Simulate AISamplingProvider's other methods so isinstance checks pass.
    ai.has_profile = MagicMock(return_value=False)
    return ai


async def test_audio_tags_disabled_by_default(backend: ElevenLabsTTS) -> None:
    """Off by default — synthesize() must send the raw text untouched
    even when an AI provider is wired up."""
    await backend.initialize({"api_key": "sk-test"})
    backend.set_ai_sampling(_stub_ai("[excited] Hello!"))

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(
                text="Hello there, this is a moderately long sentence.",
                voice_id="v1",
            )
        )
        assert (
            mock_post.call_args.kwargs["json"]["text"]
            == "Hello there, this is a moderately long sentence."
        )
    await backend.close()


async def test_audio_tags_injected_when_enabled(backend: ElevenLabsTTS) -> None:
    """When enabled, the AI's tagged output must be what reaches the
    ElevenLabs API — not the original text."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    ai = _stub_ai("[excited] Hello there!")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hello there!", voice_id="v1"))
        assert (
            mock_post.call_args.kwargs["json"]["text"] == "[excited] Hello there!"
        )

    ai.complete_one_shot.assert_awaited_once()
    await backend.close()


async def test_audio_tags_skip_short_text(backend: ElevenLabsTTS) -> None:
    """Inputs under the configured min_chars threshold bypass the AI
    call — the latency cost isn't worth it for one-liners."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 50}
    )
    ai = _stub_ai("[excited] Hi!")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text="Hi!", voice_id="v1"))
        assert mock_post.call_args.kwargs["json"]["text"] == "Hi!"

    ai.complete_one_shot.assert_not_awaited()
    await backend.close()


async def test_audio_tags_skip_pretagged_input(backend: ElevenLabsTTS) -> None:
    """If the caller authored tags by hand, respect them — never
    re-run them through the director (which might add or strip tags
    inconsistently)."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    ai = _stub_ai("[wrong] tags")
    backend.set_ai_sampling(ai)
    pre_tagged = "[whispers] this is already tagged, leave it alone."

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(SynthesisRequest(text=pre_tagged, voice_id="v1"))
        assert mock_post.call_args.kwargs["json"]["text"] == pre_tagged

    ai.complete_one_shot.assert_not_awaited()
    await backend.close()


async def test_audio_tags_fall_back_when_response_diverges(
    backend: ElevenLabsTTS,
) -> None:
    """If the director model goes off-rails and returns something that
    isn't the input with tags (e.g. an instruction-following meta-reply),
    fall back to raw text instead of speaking the hallucination."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    # Real-world failure mode: input is a time-of-day response, model
    # replies with "I'm ready to tag text, please provide the input."
    ai = _stub_ai(
        "I understand. I'm ready to tag text for ElevenLabs v3 audio with "
        "performance cues. Please provide the text you'd like me to add tags to."
    )
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(
                text="It's currently 9:36 PM PDT on Sunday, May 17th, 2026.",
                voice_id="v1",
            )
        )
        # The hallucinated meta-reply MUST NOT be what reaches the API.
        sent = mock_post.call_args.kwargs["json"]["text"]
        assert sent == "It's currently 9:36 PM PDT on Sunday, May 17th, 2026."

    await backend.close()


async def test_audio_tags_accept_tagged_response_with_high_overlap(
    backend: ElevenLabsTTS,
) -> None:
    """A response that's clearly the input with tags inserted passes the
    divergence check (regression — make sure the validator isn't too strict)."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    ai = _stub_ai(
        "[curious] Mount Everest is the largest mountain in the world, "
        "standing at 29,032 feet above sea level. [calm] It's located in "
        "the Himalayas on the border between Nepal and Tibet."
    )
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        await backend.synthesize(
            SynthesisRequest(
                text=(
                    "Mount Everest is the largest mountain in the world, "
                    "standing at 29,032 feet above sea level. It's located in "
                    "the Himalayas on the border between Nepal and Tibet."
                ),
                voice_id="v1",
            )
        )
        sent = mock_post.call_args.kwargs["json"]["text"]
        assert sent.startswith("[curious]")

    await backend.close()


async def test_audio_tags_fallback_on_ai_error(backend: ElevenLabsTTS) -> None:
    """If the AI call blows up, synthesize() still succeeds with the
    raw text — TTS must never depend on the director being healthy."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    ai = AsyncMock()
    ai.complete_one_shot = AsyncMock(side_effect=RuntimeError("director offline"))
    ai.has_profile = MagicMock(return_value=False)
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ) as mock_post:
        result = await backend.synthesize(
            SynthesisRequest(text="hello world, raw text expected", voice_id="v1")
        )
        assert (
            mock_post.call_args.kwargs["json"]["text"]
            == "hello world, raw text expected"
        )

    assert result.audio == b"audio-bytes"
    await backend.close()


async def test_audio_tags_cache_reused_for_same_input(
    backend: ElevenLabsTTS,
) -> None:
    """Repeated identical inputs hit the tag cache — Haiku should run
    once even if synthesize() is called many times."""
    await backend.initialize(
        {"api_key": "sk-test", "enable_audio_tags": True, "audio_tag_min_chars": 0}
    )
    ai = _stub_ai("[curious] Same phrase again")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        for _ in range(3):
            await backend.synthesize(
                SynthesisRequest(text="Same phrase again", voice_id="v1")
            )

    assert ai.complete_one_shot.await_count == 1
    await backend.close()


async def test_audio_tags_cache_invalidates_on_prompt_change(
    backend: ElevenLabsTTS,
) -> None:
    """Editing the system prompt and re-initializing must invalidate
    cached tag entries — otherwise the new prompt's behavior would be
    invisible until the LRU evicted old entries naturally."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_system_prompt": "PROMPT_A",
        }
    )
    ai = _stub_ai("[excited] tagged-by-A")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="phrase one", voice_id="v1"))

    # Re-init with a new prompt — close + initialize again to simulate
    # what the live-config-reload path does in real ops.
    await backend.close()
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_system_prompt": "PROMPT_B",
        }
    )
    ai_b = _stub_ai("[sad] tagged-by-B")
    backend.set_ai_sampling(ai_b)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(SynthesisRequest(text="phrase one", voice_id="v1"))

    ai_b.complete_one_shot.assert_awaited_once()
    await backend.close()


async def test_audio_tag_custom_system_prompt_reaches_ai(
    backend: ElevenLabsTTS,
) -> None:
    """The configured system prompt is what the AI receives, not the
    built-in default — verifying the field is actually plumbed."""
    custom = "Be terse. Tag only with [whispers]."
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_system_prompt": custom,
        }
    )
    ai = _stub_ai("[whispers] hi")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="long enough to inject", voice_id="v1")
        )

    assert ai.complete_one_shot.await_args.kwargs["system_prompt"] == custom
    await backend.close()


async def test_audio_tag_blank_prompt_falls_back_to_default(
    backend: ElevenLabsTTS,
) -> None:
    """Blanking the field in Settings must NOT send an empty system
    prompt to the AI — fall back to the built-in default instead."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_system_prompt": "   ",
        }
    )
    ai = _stub_ai("[excited] hi")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="long enough to inject", voice_id="v1")
        )

    sent = ai.complete_one_shot.await_args.kwargs["system_prompt"]
    assert "ElevenLabs v3 audio tags" in sent
    await backend.close()


async def test_audio_tag_profile_passed_to_ai(backend: ElevenLabsTTS) -> None:
    """The configured ``audio_tag_profile`` must reach the AI call so
    callers can opt into profile-level guardrails (cost tracking,
    role overrides, tool whitelists)."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_profile": "fast_pipeline",
        }
    )
    ai = _stub_ai("[excited] hi")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="long enough to inject", voice_id="v1")
        )

    assert (
        ai.complete_one_shot.await_args.kwargs["profile_name"] == "fast_pipeline"
    )
    await backend.close()


async def test_audio_tag_profile_blank_means_no_profile(backend: ElevenLabsTTS) -> None:
    """When ``audio_tag_profile`` is blank, ``profile_name=None`` should
    reach ``complete_one_shot`` — not the literal empty string, which
    would look like a profile lookup that always misses."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
        }
    )
    ai = _stub_ai("[excited] hi")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="long enough to inject", voice_id="v1")
        )

    assert ai.complete_one_shot.await_args.kwargs["profile_name"] is None
    await backend.close()


async def test_audio_tag_context_wraps_user_message(
    backend: ElevenLabsTTS,
) -> None:
    """When the SynthesisRequest carries a ``context``, the user
    message sent to the director must be the rendered template — not
    the raw text — so the model can take the situation into account."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
        }
    )
    ai = _stub_ai("[curious] Did you find the wrench?")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(
                text="Did you find the wrench?",
                voice_id="v1",
                context="Asking a coworker who's been searching the toolbox",
            )
        )

    sent = ai.complete_one_shot.await_args.kwargs["messages"][0].content
    assert "Did you find the wrench?" in sent
    assert "Asking a coworker who's been searching the toolbox" in sent
    assert sent.startswith("Context:")


async def test_audio_tag_no_context_sends_raw_text(
    backend: ElevenLabsTTS,
) -> None:
    """No context means no template wrapping — the user message is
    just the raw text. Otherwise we'd waste tokens on empty Context:
    headers and confuse the director's parsing."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
        }
    )
    ai = _stub_ai("[neutral] Just a sentence.")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="Just a sentence.", voice_id="v1")
        )

    sent = ai.complete_one_shot.await_args.kwargs["messages"][0].content
    assert sent == "Just a sentence."


async def test_audio_tag_context_cache_keys_include_context(
    backend: ElevenLabsTTS,
) -> None:
    """The same text under two different contexts should produce two
    Haiku calls and two cache entries — one tagged-text per context.
    Otherwise switching context wouldn't change the delivery."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
        }
    )
    ai = _stub_ai("[neutral] Same text")
    backend.set_ai_sampling(ai)

    with patch.object(
        backend._client,  # type: ignore[union-attr]
        "post",
        return_value=_make_mock_response(),
    ):
        await backend.synthesize(
            SynthesisRequest(text="Same text", voice_id="v1", context="happy")
        )
        await backend.synthesize(
            SynthesisRequest(text="Same text", voice_id="v1", context="angry")
        )
        # Repeating the first context should hit the tag cache.
        await backend.synthesize(
            SynthesisRequest(text="Same text", voice_id="v1", context="happy")
        )

    assert ai.complete_one_shot.await_count == 2
    await backend.close()


async def test_audio_tag_invalid_template_falls_back_to_raw(
    backend: ElevenLabsTTS,
) -> None:
    """A template missing required placeholders is rejected at config
    load — synthesize() proceeds with the default template, so a bad
    config doesn't take TTS down."""
    await backend.initialize(
        {
            "api_key": "sk-test",
            "enable_audio_tags": True,
            "audio_tag_min_chars": 0,
            "audio_tag_context_template": "no placeholders here",
        }
    )
    # The bad template was rejected; the default is in effect now.
    assert "{context}" in backend._audio_tag_context_template
    assert "{text}" in backend._audio_tag_context_template
    await backend.close()
