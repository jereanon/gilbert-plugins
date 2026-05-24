"""Tests for the Kokoro TTS backend."""

from __future__ import annotations

import pytest

from gilbert.interfaces.tts import Voice


def test_module_imports() -> None:
    """The package shim from conftest.py makes the plugin importable."""
    import gilbert_plugin_kokoro  # noqa: F401


def test_voice_catalog_is_nonempty() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _VOICES

    assert len(_VOICES) >= 20
    assert all(isinstance(v, Voice) for v in _VOICES)


def test_voice_catalog_unique_ids() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _VOICES

    ids = [v.voice_id for v in _VOICES]
    assert len(ids) == len(set(ids)), "voice_id must be unique across the catalog"


def test_voice_catalog_labels_populated() -> None:
    """Every voice has language, region, and gender labels for filtering."""
    from gilbert_plugin_kokoro.kokoro_tts import _VOICES

    for v in _VOICES:
        assert v.labels.get("language"), f"missing language label on {v.voice_id}"
        assert v.labels.get("gender") in ("female", "male"), f"bad gender on {v.voice_id}"


@pytest.mark.parametrize(
    "voice_id, expected_lang_code",
    [
        ("af_heart", "a"),
        ("am_adam", "a"),
        ("bf_emma", "b"),
        ("bm_george", "b"),
        ("jf_alpha", "j"),
        ("jm_kumo", "j"),
        ("zf_xiaoxiao", "z"),
        ("zm_yunjian", "z"),
        ("ef_dora", "e"),
        ("em_alex", "e"),
        ("ff_siwis", "f"),
        ("hf_alpha", "h"),
        ("hm_omega", "h"),
        ("if_sara", "i"),
        ("im_nicola", "i"),
        ("pf_dora", "p"),
        ("pm_alex", "p"),
    ],
)
def test_voice_id_first_char_encodes_lang_code(voice_id: str, expected_lang_code: str) -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _lang_code_for_voice

    assert _lang_code_for_voice(voice_id) == expected_lang_code


from gilbert.interfaces.tts import TTSBackend


def test_backend_registered() -> None:
    """Importing the module registers the backend in the ABC's registry."""
    import gilbert_plugin_kokoro.kokoro_tts  # noqa: F401
    backends = TTSBackend.registered_backends()
    assert "kokoro" in backends


def test_backend_config_params_keys() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    params = KokoroTTSBackend.backend_config_params()
    keys = [p.key for p in params]
    assert keys == ["device", "default_voice", "speed", "preload"]


def test_backend_config_param_defaults() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    by_key = {p.key: p for p in KokoroTTSBackend.backend_config_params()}
    assert by_key["device"].default == "cpu"
    assert by_key["device"].choices == ("cpu", "cuda", "mps", "auto")
    assert by_key["device"].restart_required is True
    assert by_key["default_voice"].default == "af_heart"
    assert by_key["default_voice"].choices is not None
    assert "af_heart" in by_key["default_voice"].choices
    assert "jf_alpha" in by_key["default_voice"].choices
    assert by_key["speed"].default == 1.0
    assert by_key["preload"].default is False
    assert by_key["preload"].restart_required is True


async def test_list_voices_returns_catalog() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend, _VOICES

    backend = KokoroTTSBackend()
    voices = await backend.list_voices()
    assert voices == _VOICES


async def test_get_voice_known() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    v = await backend.get_voice("af_heart")
    assert v is not None
    assert v.voice_id == "af_heart"
    assert v.labels["gender"] == "female"


async def test_get_voice_unknown() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    v = await backend.get_voice("nope")
    assert v is None


async def test_initialize_stores_config() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({
        "device": "cuda",
        "default_voice": "bm_george",
        "speed": 1.25,
        "preload": False,
    })
    assert backend._device == "cuda"
    assert backend._default_voice == "bm_george"
    assert backend._speed == 1.25
    assert backend._preload is False


async def test_initialize_defaults_when_keys_missing() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({})
    assert backend._device == "cpu"
    assert backend._default_voice == "af_heart"
    assert backend._speed == 1.0
    assert backend._preload is False


async def test_close_clears_pipelines() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({})
    backend._pipelines["a"] = object()  # simulate a cached pipeline
    await backend.close()
    assert backend._pipelines == {}


import numpy as np

from gilbert.interfaces.tts import AudioFormat


def _fake_pcm(seconds: float = 0.25, freq: float = 440.0, sr: int = 24000) -> np.ndarray:
    """Generate a short float32 sine wave for encoder testing."""
    t = np.arange(int(seconds * sr)) / sr
    return (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_encode_wav_starts_with_riff() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _encode

    out = _encode(_fake_pcm(), AudioFormat.WAV)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"


def test_encode_mp3_has_mp3_magic() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _encode

    out = _encode(_fake_pcm(), AudioFormat.MP3)
    # MP3 streams start with either an ID3 tag (b"ID3") or an MPEG
    # frame sync (b"\xff\xfb" / b"\xff\xfa" / b"\xff\xf3" / b"\xff\xf2").
    assert out[:3] == b"ID3" or (out[0] == 0xFF and (out[1] & 0xE0) == 0xE0)
    assert len(out) > 100


def test_encode_ogg_starts_with_oggs() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _encode

    out = _encode(_fake_pcm(), AudioFormat.OGG)
    assert out[:4] == b"OggS"


def test_encode_pcm_is_int16_at_44100() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _encode

    out = _encode(_fake_pcm(seconds=1.0), AudioFormat.PCM)
    # 1 second of mono int16 at 44100 Hz = 88200 bytes.
    # PyAV resampling may produce slight off-by-one due to fractional
    # rates, so allow a few samples of slack.
    assert 88000 <= len(out) <= 88400
    samples = np.frombuffer(out, dtype="<i2")
    # Non-silent — at least one sample is well above zero.
    assert int(np.max(np.abs(samples))) > 1000


def test_encode_empty_input_returns_short_output() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import _encode

    out = _encode(np.zeros(0, dtype=np.float32), AudioFormat.WAV)
    # Header-only WAV is OK; just don't crash.
    assert out[:4] == b"RIFF"


from unittest.mock import MagicMock, patch

from gilbert.interfaces.tts import SynthesisRequest


def _mock_pipeline_yielding(samples_per_chunk: list[int]):
    """Build a mock KPipeline whose call returns float32 chunks."""
    rng = np.random.default_rng(0)
    chunks = [
        (None, None, rng.standard_normal(n).astype(np.float32) * 0.1)
        for n in samples_per_chunk
    ]
    pipeline = MagicMock()
    pipeline.return_value = iter(chunks)
    return pipeline


async def test_synthesize_uses_pipeline_for_voice_lang() -> None:
    from gilbert_plugin_kokoro import kokoro_tts as kt

    backend = kt.KokoroTTSBackend()
    await backend.initialize({})
    fake_pipeline = _mock_pipeline_yielding([2400, 2400])  # 0.2s of audio

    with patch.object(kt, "_build_pipeline", return_value=fake_pipeline) as build:
        request = SynthesisRequest(
            text="Hello world.",
            voice_id="af_heart",
            output_format=AudioFormat.MP3,
        )
        result = await backend.synthesize(request)

    build.assert_called_once_with("a", "cpu")
    fake_pipeline.assert_called_once()
    call_kwargs = fake_pipeline.call_args.kwargs
    call_args = fake_pipeline.call_args.args
    assert "Hello world." in (call_args + tuple(call_kwargs.values()))
    assert call_kwargs.get("voice") == "af_heart"
    assert call_kwargs.get("speed") == 1.0

    assert result.format == AudioFormat.MP3
    assert result.audio[:3] == b"ID3" or result.audio[0] == 0xFF
    assert result.characters_used == len("Hello world.")


async def test_synthesize_caches_pipeline_per_language() -> None:
    from gilbert_plugin_kokoro import kokoro_tts as kt

    backend = kt.KokoroTTSBackend()
    await backend.initialize({})
    pipeline_a = _mock_pipeline_yielding([2400])
    pipeline_b = _mock_pipeline_yielding([2400])

    def _build(lang_code: str, device: str):
        return pipeline_a if lang_code == "a" else pipeline_b

    pipeline_a.return_value = iter(
        [(None, None, np.zeros(2400, dtype=np.float32))]
    )
    with patch.object(kt, "_build_pipeline", side_effect=_build) as build:
        await backend.synthesize(SynthesisRequest(text="hi", voice_id="af_heart"))
        pipeline_a.return_value = iter(
            [(None, None, np.zeros(2400, dtype=np.float32))]
        )
        await backend.synthesize(SynthesisRequest(text="hi", voice_id="am_adam"))
        await backend.synthesize(SynthesisRequest(text="hi", voice_id="bf_emma"))

    assert build.call_count == 2
    assert {c.args[0] for c in build.call_args_list} == {"a", "b"}


async def test_synthesize_uses_request_voice_speed_format() -> None:
    from gilbert_plugin_kokoro import kokoro_tts as kt

    backend = kt.KokoroTTSBackend()
    await backend.initialize({"speed": 1.5})
    fake_pipeline = _mock_pipeline_yielding([2400])

    with patch.object(kt, "_build_pipeline", return_value=fake_pipeline):
        request = SynthesisRequest(
            text="x",
            voice_id="bm_george",
            output_format=AudioFormat.WAV,
            speed=0.75,
        )
        result = await backend.synthesize(request)

    assert fake_pipeline.call_args.kwargs.get("speed") == 0.75
    assert result.format == AudioFormat.WAV
    assert result.audio[:4] == b"RIFF"


async def test_synthesize_unknown_voice_raises_valueerror() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({})
    with pytest.raises(ValueError, match="Unknown Kokoro voice"):
        await backend.synthesize(
            SynthesisRequest(text="x", voice_id="xx_nope")
        )


async def test_synthesize_preload_builds_default_lang_pipeline() -> None:
    """preload=True should build the default-voice's pipeline in initialize()."""
    from gilbert_plugin_kokoro import kokoro_tts as kt

    backend = kt.KokoroTTSBackend()
    with patch.object(kt, "_build_pipeline", return_value=MagicMock()) as build:
        await backend.initialize({"preload": True, "default_voice": "jf_alpha"})
    build.assert_called_once_with("j", "cpu")
    assert "j" in backend._pipelines


def test_plugin_metadata() -> None:
    from gilbert_plugin_kokoro.plugin import KokoroPlugin

    meta = KokoroPlugin().metadata()
    assert meta.name == "kokoro"
    assert "kokoro_tts" in meta.provides
    assert meta.requires == []


def test_plugin_runtime_dependencies() -> None:
    from gilbert_plugin_kokoro.plugin import KokoroPlugin

    deps = KokoroPlugin().runtime_dependencies()
    assert len(deps) == 1
    dep = deps[0]
    assert "kokoro" in dep.name.lower() or "tts" in dep.name.lower()
    # The check actually exercises kokoro+av, not just `which python`.
    assert "kokoro" in dep.check_cmd
    assert "av" in dep.check_cmd
    assert dep.install_hint  # non-empty hint


def test_create_plugin_returns_kokoro_plugin() -> None:
    from gilbert_plugin_kokoro.plugin import KokoroPlugin, create_plugin

    p = create_plugin()
    assert isinstance(p, KokoroPlugin)
