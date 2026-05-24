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
