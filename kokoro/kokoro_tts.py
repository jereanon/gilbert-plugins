"""Kokoro TTS backend — local synthesis via the kokoro package."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    Voice,
)

logger = logging.getLogger(__name__)


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
    _v("af_alloy", "Alloy", "en-US", "American", "female"),
    _v("af_aoede", "Aoede", "en-US", "American", "female"),
    _v("af_bella", "Bella", "en-US", "American", "female"),
    _v("af_heart", "Heart", "en-US", "American", "female"),
    _v("af_jessica", "Jessica", "en-US", "American", "female"),
    _v("af_kore", "Kore", "en-US", "American", "female"),
    _v("af_nicole", "Nicole", "en-US", "American", "female"),
    _v("af_nova", "Nova", "en-US", "American", "female"),
    _v("af_river", "River", "en-US", "American", "female"),
    _v("af_sarah", "Sarah", "en-US", "American", "female"),
    _v("af_sky", "Sky", "en-US", "American", "female"),
    _v("am_adam", "Adam", "en-US", "American", "male"),
    _v("am_echo", "Echo", "en-US", "American", "male"),
    _v("am_eric", "Eric", "en-US", "American", "male"),
    _v("am_fenrir", "Fenrir", "en-US", "American", "male"),
    _v("am_liam", "Liam", "en-US", "American", "male"),
    _v("am_michael", "Michael", "en-US", "American", "male"),
    _v("am_onyx", "Onyx", "en-US", "American", "male"),
    _v("am_puck", "Puck", "en-US", "American", "male"),
    _v("am_santa", "Santa", "en-US", "American", "male"),
    # British English (b)
    _v("bf_alice", "Alice", "en-GB", "British", "female"),
    _v("bf_emma", "Emma", "en-GB", "British", "female"),
    _v("bf_isabella", "Isabella", "en-GB", "British", "female"),
    _v("bf_lily", "Lily", "en-GB", "British", "female"),
    _v("bm_daniel", "Daniel", "en-GB", "British", "male"),
    _v("bm_fable", "Fable", "en-GB", "British", "male"),
    _v("bm_george", "George", "en-GB", "British", "male"),
    _v("bm_lewis", "Lewis", "en-GB", "British", "male"),
    # Japanese (j)
    _v("jf_alpha", "Alpha", "ja", "Japan", "female"),
    _v("jf_gongitsune", "Gongitsune", "ja", "Japan", "female"),
    _v("jf_nezumi", "Nezumi", "ja", "Japan", "female"),
    _v("jf_tebukuro", "Tebukuro", "ja", "Japan", "female"),
    _v("jm_kumo", "Kumo", "ja", "Japan", "male"),
    # Mandarin Chinese (z)
    _v("zf_xiaobei", "Xiaobei", "zh", "Mainland", "female"),
    _v("zf_xiaoni", "Xiaoni", "zh", "Mainland", "female"),
    _v("zf_xiaoxiao", "Xiaoxiao", "zh", "Mainland", "female"),
    _v("zf_xiaoyi", "Xiaoyi", "zh", "Mainland", "female"),
    _v("zm_yunjian", "Yunjian", "zh", "Mainland", "male"),
    _v("zm_yunxi", "Yunxi", "zh", "Mainland", "male"),
    _v("zm_yunxia", "Yunxia", "zh", "Mainland", "male"),
    _v("zm_yunyang", "Yunyang", "zh", "Mainland", "male"),
    # Spanish (e)
    _v("ef_dora", "Dora", "es", "Spain", "female"),
    _v("em_alex", "Alex", "es", "Spain", "male"),
    _v("em_santa", "Santa", "es", "Spain", "male"),
    # French (f)
    _v("ff_siwis", "Siwis", "fr", "France", "female"),
    # Hindi (h)
    _v("hf_alpha", "Alpha", "hi", "India", "female"),
    _v("hf_beta", "Beta", "hi", "India", "female"),
    _v("hm_omega", "Omega", "hi", "India", "male"),
    _v("hm_psi", "Psi", "hi", "India", "male"),
    # Italian (i)
    _v("if_sara", "Sara", "it", "Italy", "female"),
    _v("im_nicola", "Nicola", "it", "Italy", "male"),
    # Portuguese (p)
    _v("pf_dora", "Dora", "pt", "Brazil", "female"),
    _v("pm_alex", "Alex", "pt", "Brazil", "male"),
    _v("pm_santa", "Santa", "pt", "Brazil", "male"),
]


_VOICES_BY_ID: dict[str, Voice] = {v.voice_id: v for v in _VOICES}


def _lang_code_for_voice(voice_id: str) -> str:
    """Return the kokoro KPipeline lang_code for a voice ID.

    The first character of the voice_id is the lang code (a/b/j/z/e/f/h/i/p).
    """
    if not voice_id:
        raise ValueError("voice_id is empty")
    return voice_id[0]


_OUT_SAMPLE_RATE = 44100  # matches interfaces/tts.py _PCM_SAMPLE_RATE


def _encode(samples_24k_f32: np.ndarray, fmt: AudioFormat) -> bytes:
    """Resample float32 24kHz mono to 44.1kHz mono int16 and encode.

    PCM returns raw little-endian int16 bytes. WAV/MP3/OGG are produced
    by PyAV's in-memory muxer. All output is mono.
    """
    import av  # local import: heavy dep, only needed at synthesis time

    # Resample 24000 -> 44100 in float32 using PyAV's audio resampler.
    src_layout = "mono"
    src_format = "flt"

    if samples_24k_f32.size == 0:
        resampled = np.zeros(0, dtype=np.int16)
    else:
        in_frame = av.AudioFrame.from_ndarray(
            samples_24k_f32.reshape(1, -1),
            format=src_format,
            layout=src_layout,
        )
        in_frame.sample_rate = 24000
        resampler = av.AudioResampler(format="s16", layout=src_layout, rate=_OUT_SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        for out_frame in resampler.resample(in_frame):
            chunks.append(out_frame.to_ndarray().reshape(-1))
        # Flush.
        for out_frame in resampler.resample(None):
            chunks.append(out_frame.to_ndarray().reshape(-1))
        resampled = (
            np.concatenate(chunks).astype(np.int16) if chunks else np.zeros(0, dtype=np.int16)
        )

    if fmt == AudioFormat.PCM:
        return resampled.tobytes()

    # Mux to MP3 / WAV / OGG via PyAV.
    codec_for_format = {
        AudioFormat.MP3: ("mp3", "libmp3lame"),
        AudioFormat.WAV: ("wav", "pcm_s16le"),
        AudioFormat.OGG: ("ogg", "libvorbis"),
    }
    container_fmt, codec_name = codec_for_format[fmt]

    # Ensure at least one silent sample so the muxer can write a valid header.
    if resampled.size == 0:
        resampled = np.zeros(1, dtype=np.int16)

    buf = io.BytesIO()
    output = av.open(buf, mode="w", format=container_fmt)
    try:
        stream = output.add_stream(codec_name, rate=_OUT_SAMPLE_RATE)
        stream.layout = "mono"  # type: ignore[union-attr]

        # Re-frame as int16 mono at 44.1kHz so the encoder accepts it.
        frame = av.AudioFrame.from_ndarray(
            resampled.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = _OUT_SAMPLE_RATE
        for packet in stream.encode(frame):  # type: ignore[union-attr,arg-type]
            output.mux(packet)
        for packet in stream.encode(None):  # type: ignore[union-attr]
            output.mux(packet)
    finally:
        output.close()
    return buf.getvalue()


# Sentence-splitter: terminal . ! ? optionally followed by quote, then
# whitespace, OR end-of-string. Trailing fragments without terminal
# punctuation are kept as a final chunk. Tuned for English; non-English
# voices still produce sensible-enough boundaries since the regex
# matches the same Latin punctuation other Kokoro languages use.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])["\'\)\]]?\s+')


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into a list of non-empty sentence-ish chunks."""
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def _build_pipeline(lang_code: str, device: str) -> Any:
    """Construct a kokoro.KPipeline. Isolated so tests can patch it."""
    from kokoro import KPipeline  # type: ignore[attr-defined]  # kokoro has no py.typed stubs

    # device="auto" lets kokoro pick — pass-through otherwise.
    if device == "auto":
        return KPipeline(lang_code=lang_code)
    return KPipeline(lang_code=lang_code, device=device)


class KokoroTTSBackend(TTSBackend):
    """Local TTS via the open-weights Kokoro-82M model.

    Lazily instantiates one ``kokoro.KPipeline`` per language code on
    first use and caches them. Synthesis runs in a thread executor
    because kokoro is sync/blocking. Output is always resampled to
    44.1 kHz mono int16 before encoding to the caller's requested
    AudioFormat via PyAV.
    """

    backend_name = "kokoro"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="device",
                type=ToolParameterType.STRING,
                description="Inference device.",
                default="cpu",
                choices=("cpu", "cuda", "mps", "auto"),
                restart_required=True,
            ),
            ConfigParam(
                key="default_voice",
                type=ToolParameterType.STRING,
                description="Voice ID used when the caller does not specify one.",
                default="af_heart",
                choices=tuple(v.voice_id for v in _VOICES),
            ),
            ConfigParam(
                key="speed",
                type=ToolParameterType.NUMBER,
                description="Default speech rate multiplier (0.5 = slow, 2.0 = fast).",
                default=1.0,
            ),
            ConfigParam(
                key="preload",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Load the default-language Kokoro pipeline at startup. "
                    "When false (default), the model loads on the first "
                    "synthesis request, which adds ~5-10 s to that call."
                ),
                default=False,
                restart_required=True,
            ),
        ]

    def __init__(self) -> None:
        self._device: str = "cpu"
        self._default_voice: str = "af_heart"
        self._speed: float = 1.0
        self._preload: bool = False
        self._pipelines: dict[str, Any] = {}

    async def initialize(self, config: dict[str, object]) -> None:
        self._device = str(config.get("device", "cpu"))
        self._default_voice = str(config.get("default_voice", "af_heart"))
        self._speed = float(config.get("speed", 1.0))  # type: ignore[arg-type]
        self._preload = bool(config.get("preload", False))
        logger.info(
            "KokoroTTSBackend initialized: device=%s default_voice=%s speed=%s preload=%s",
            self._device,
            self._default_voice,
            self._speed,
            self._preload,
        )
        if self._preload:
            lang = _lang_code_for_voice(self._default_voice)
            self._pipelines[lang] = _build_pipeline(lang, self._device)

    async def close(self) -> None:
        self._pipelines.clear()

    def _get_pipeline(self, lang_code: str) -> Any:
        pipeline = self._pipelines.get(lang_code)
        if pipeline is None:
            pipeline = _build_pipeline(lang_code, self._device)
            self._pipelines[lang_code] = pipeline
        return pipeline

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if request.voice_id not in _VOICES_BY_ID:
            raise ValueError(f"Unknown Kokoro voice: {request.voice_id!r}")
        lang = _lang_code_for_voice(request.voice_id)
        pipeline = self._get_pipeline(lang)
        speed = float(request.speed) if request.speed else self._speed

        loop = asyncio.get_running_loop()

        def _run_sync() -> np.ndarray:
            chunks: list[np.ndarray] = []
            for _g, _p, audio in pipeline(request.text, voice=request.voice_id, speed=speed):
                arr = np.asarray(audio, dtype=np.float32).reshape(-1)
                chunks.append(arr)
            if not chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(chunks)

        samples = await loop.run_in_executor(None, _run_sync)
        audio_bytes = _encode(samples, request.output_format)
        duration = float(samples.size) / 24000.0 if samples.size else 0.0
        return SynthesisResult(
            audio=audio_bytes,
            format=request.output_format,
            duration_seconds=duration,
            characters_used=len(request.text),
        )

    def synthesize_stream(
        self, request: SynthesisRequest,
    ) -> AsyncIterator[bytes]:
        """Stream audio sentence-by-sentence.

        Splits the input text on sentence boundaries and yields each
        sentence's encoded audio as a separate chunk. The speaker hears
        sentence 1 while sentence 2 renders, which materially improves
        perceived latency on long replies — even though kokoro is local
        CPU inference."""
        if request.voice_id not in _VOICES_BY_ID:
            raise ValueError(f"Unknown Kokoro voice: {request.voice_id!r}")
        lang = _lang_code_for_voice(request.voice_id)
        speed = float(request.speed) if request.speed else self._speed
        sentences = _split_sentences(request.text) or [request.text]

        async def _gen() -> AsyncIterator[bytes]:
            pipeline = self._get_pipeline(lang)
            loop = asyncio.get_running_loop()
            for sentence in sentences:
                def _run_sync(s: str = sentence) -> np.ndarray:
                    chunks: list[np.ndarray] = []
                    for _g, _p, audio in pipeline(s, voice=request.voice_id, speed=speed):
                        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
                        chunks.append(arr)
                    if not chunks:
                        return np.zeros(0, dtype=np.float32)
                    return np.concatenate(chunks)
                samples = await loop.run_in_executor(None, _run_sync)
                yield _encode(samples, request.output_format)

        return _gen()

    async def list_voices(self) -> list[Voice]:
        return list(_VOICES)

    async def get_voice(self, voice_id: str) -> Voice | None:
        return _VOICES_BY_ID.get(voice_id)
