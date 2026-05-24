"""Kokoro TTS backend — local synthesis via the kokoro package."""

from __future__ import annotations

from gilbert.interfaces.tts import Voice


def _v(voice_id: str, name: str, language: str, region: str, gender: str) -> Voice:
    return Voice(
        voice_id=voice_id,
        name=name,
        language=language,
        labels={"language": language, "region": region, "gender": gender},
    )


# Kokoro v1.0 voice catalog. The first character of voice_id encodes the
# language pipeline (a=American English, b=British, j=Japanese, z=Chinese,
# e=Spanish, f=French, h=Hindi, i=Italian, p=Portuguese). The second
# character is gender (f=female, m=male).
_VOICES: list[Voice] = [
    # American English (a)
    _v("af_alloy",   "Alloy",   "en-US", "American", "female"),
    _v("af_aoede",   "Aoede",   "en-US", "American", "female"),
    _v("af_bella",   "Bella",   "en-US", "American", "female"),
    _v("af_heart",   "Heart",   "en-US", "American", "female"),
    _v("af_jessica", "Jessica", "en-US", "American", "female"),
    _v("af_kore",    "Kore",    "en-US", "American", "female"),
    _v("af_nicole",  "Nicole",  "en-US", "American", "female"),
    _v("af_nova",    "Nova",    "en-US", "American", "female"),
    _v("af_river",   "River",   "en-US", "American", "female"),
    _v("af_sarah",   "Sarah",   "en-US", "American", "female"),
    _v("af_sky",     "Sky",     "en-US", "American", "female"),
    _v("am_adam",    "Adam",    "en-US", "American", "male"),
    _v("am_echo",    "Echo",    "en-US", "American", "male"),
    _v("am_eric",    "Eric",    "en-US", "American", "male"),
    _v("am_fenrir",  "Fenrir",  "en-US", "American", "male"),
    _v("am_liam",    "Liam",    "en-US", "American", "male"),
    _v("am_michael", "Michael", "en-US", "American", "male"),
    _v("am_onyx",    "Onyx",    "en-US", "American", "male"),
    _v("am_puck",    "Puck",    "en-US", "American", "male"),
    _v("am_santa",   "Santa",   "en-US", "American", "male"),
    # British English (b)
    _v("bf_alice",    "Alice",    "en-GB", "British", "female"),
    _v("bf_emma",     "Emma",     "en-GB", "British", "female"),
    _v("bf_isabella", "Isabella", "en-GB", "British", "female"),
    _v("bf_lily",     "Lily",     "en-GB", "British", "female"),
    _v("bm_daniel",   "Daniel",   "en-GB", "British", "male"),
    _v("bm_fable",    "Fable",    "en-GB", "British", "male"),
    _v("bm_george",   "George",   "en-GB", "British", "male"),
    _v("bm_lewis",    "Lewis",    "en-GB", "British", "male"),
    # Japanese (j)
    _v("jf_alpha",    "Alpha",    "ja", "Japan", "female"),
    _v("jf_gongitsune", "Gongitsune", "ja", "Japan", "female"),
    _v("jf_nezumi",   "Nezumi",   "ja", "Japan", "female"),
    _v("jf_tebukuro", "Tebukuro", "ja", "Japan", "female"),
    _v("jm_kumo",     "Kumo",     "ja", "Japan", "male"),
    # Mandarin Chinese (z)
    _v("zf_xiaobei",  "Xiaobei",  "zh", "Mainland", "female"),
    _v("zf_xiaoni",   "Xiaoni",   "zh", "Mainland", "female"),
    _v("zf_xiaoxiao", "Xiaoxiao", "zh", "Mainland", "female"),
    _v("zf_xiaoyi",   "Xiaoyi",   "zh", "Mainland", "female"),
    _v("zm_yunjian",  "Yunjian",  "zh", "Mainland", "male"),
    _v("zm_yunxi",    "Yunxi",    "zh", "Mainland", "male"),
    _v("zm_yunxia",   "Yunxia",   "zh", "Mainland", "male"),
    _v("zm_yunyang",  "Yunyang",  "zh", "Mainland", "male"),
    # Spanish (e)
    _v("ef_dora",     "Dora",     "es", "Spain", "female"),
    _v("em_alex",     "Alex",     "es", "Spain", "male"),
    _v("em_santa",    "Santa",    "es", "Spain", "male"),
    # French (f)
    _v("ff_siwis",    "Siwis",    "fr", "France", "female"),
    # Hindi (h)
    _v("hf_alpha",    "Alpha",    "hi", "India", "female"),
    _v("hf_beta",     "Beta",     "hi", "India", "female"),
    _v("hm_omega",    "Omega",    "hi", "India", "male"),
    _v("hm_psi",      "Psi",      "hi", "India", "male"),
    # Italian (i)
    _v("if_sara",     "Sara",     "it", "Italy", "female"),
    _v("im_nicola",   "Nicola",   "it", "Italy", "male"),
    # Portuguese (p)
    _v("pf_dora",     "Dora",     "pt", "Brazil", "female"),
    _v("pm_alex",     "Alex",     "pt", "Brazil", "male"),
    _v("pm_santa",    "Santa",    "pt", "Brazil", "male"),
]


_VOICES_BY_ID: dict[str, Voice] = {v.voice_id: v for v in _VOICES}


def _lang_code_for_voice(voice_id: str) -> str:
    """Return the kokoro KPipeline lang_code for a voice ID.

    The first character of the voice_id is the lang code (a/b/j/z/e/f/h/i/p).
    """
    if not voice_id:
        raise ValueError("voice_id is empty")
    return voice_id[0]
