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
