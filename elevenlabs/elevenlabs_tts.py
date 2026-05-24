"""ElevenLabs TTS backend — text-to-speech via the ElevenLabs API."""

import asyncio
import base64 as _b64
import hashlib
import json as _json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSAudioChunk,
    TTSBackend,
    TTSStream,
    TTSStreamConfig,
    TTSStreamError,
    TTSWordTiming,
    Voice,
)

logger = logging.getLogger(__name__)

# Default template applied when the caller passes a non-empty
# ``context`` on the synthesis request. Wraps the raw text with the
# context as a hint for the audio-tag director model. Configurable per
# install — leave blank in Settings to fall back to this default.
_DEFAULT_AUDIO_TAG_CONTEXT_TEMPLATE = "Context: {context}\n\nText:\n{text}"

# Default minimum length (chars) for a piece of text to be worth tagging.
# Below this, the latency cost of an extra AI round-trip outweighs any
# expressiveness gain — single-word announcements ("Hi.") aren't going
# to read better with a tag.
_DEFAULT_AUDIO_TAG_MIN_CHARS = 40

# Default system prompt for the audio-tag director. Configurable per
# install via ``audio_tag_system_prompt`` so users can tune the tag
# vocabulary, density, or voice without editing code.
_DEFAULT_AUDIO_TAG_SYSTEM_PROMPT = """\
You add ElevenLabs v3 audio tags to text for TTS narration. Tags are bracketed performance cues like [excited] or [whispers] that the v3 model interprets as delivery instructions, not text to read aloud.

Tag categories and starter examples (you may use other words in these categories when they fit better):
- Emotions: [happy], [sad], [excited], [angry], [curious], [sarcastic], [nervous], [tired], [bored], [amused], [wistful], [deadpan]
- Vocal delivery: [whispers], [shouts], [soft-spoken], [conspiratorial]
- Non-verbal sounds: [laughs], [chuckles], [sighs], [gasps], [exhales], [clears throat]
- Pacing: [pauses], [hesitates], [slowly]

You may invent tags outside these examples when a more precise word fits — e.g. [resigned], [smug], [breathless]. Use single-word or short hyphenated tags. Do not use full phrases.

Rules:
- Place tags immediately before the clause or sentence they modify.
- Tags persist forward until another tag replaces them. If a strong tag like [whispers] or [angry] applies to only one sentence, follow it with a neutral tag like [neutral] or a new appropriate tag when the mood shifts.
- Don't tag every sentence. Neutral delivery is the default — only tag where the words clearly call for a specific delivery. If you're unsure, leave it untagged.
- Match the tag to what the words actually say, not the topic. A sentence about a funeral isn't automatically [sad]; it's [sad] only if the speaker sounds sad.
- Sarcasm tags require a clear textual cue (contradiction, irony, obvious setup). Don't guess.
- Never alter, add, remove, or reorder words. Preserve all original punctuation and capitalization — they're part of the delivery.
- If the user message wraps the text with a "Context: ..." block before "Text:", treat the Context as background that informs tag choice — do not echo it. Tag and return only the content under "Text:".
- Output only the tagged text. No preamble, no explanation, no code fences, no surrounding quotes.\
"""

# Pattern that detects ``[lowercase_word]`` style tags already present in
# the input. We skip the AI call entirely when the caller has authored
# tags by hand — they know what they want and shouldn't pay the
# round-trip.
_TAG_RE = re.compile(r"\[[a-z][a-z _]*\]")

# Fraction of input "content words" (length >= 3, lowercased) that must
# appear in the tag-stripped LLM response for us to trust it as a valid
# tagging of the input. Below this threshold we assume the director
# model went off-rails (e.g. "I'm ready, please provide the text…")
# and fall back to raw text so the user doesn't hear instruction-following
# slop instead of the actual content.
_AUDIO_TAG_OVERLAP_THRESHOLD = 0.7

# Tag-injector LRU bound — separate from the audio cache because the
# values here are tiny (just the tagged string). Stored as an
# OrderedDict so eviction is O(1) at the cold end.
_AUDIO_TAG_CACHE_MAX = 256


def _tagged_response_overlaps_input(input_text: str, tagged: str) -> bool:
    """Return True if ``tagged`` looks like ``input_text`` with [emotion]
    tags inserted (rather than a hallucinated meta-response).

    The director model occasionally interprets the input as a meta-question
    ("are you ready to tag text?") and replies with its own preamble instead
    of returning the tagged input. We catch that by stripping ``[…]`` tags
    from the response and checking that at least
    ``_AUDIO_TAG_OVERLAP_THRESHOLD`` of the input's content words (length
    >= 3, lowercased) are present in the stripped response.
    """
    stripped = _TAG_RE.sub(" ", tagged).lower()
    response_words: set[str] = set(re.findall(r"[a-z0-9]+", stripped))
    input_words = [
        w for w in re.findall(r"[a-z0-9]+", input_text.lower()) if len(w) >= 3
    ]
    if not input_words:
        # Nothing meaningful to compare; trust the LLM.
        return True
    hits = sum(1 for w in input_words if w in response_words)
    return (hits / len(input_words)) >= _AUDIO_TAG_OVERLAP_THRESHOLD

# ElevenLabs API base
_BASE_URL = "https://api.elevenlabs.io/v1"

# Map our AudioFormat enum to ElevenLabs output_format parameter values.
# Without an explicit entry for a given AudioFormat we silently fall
# back to MP3 — which is the wrong thing to do for telephony where
# Telnyx + carrier gear expects raw 8 kHz µ-law and will silently
# drop / mangle anything else. ALWAYS add a map entry when extending
# AudioFormat — the fallback masks real bugs.
_FORMAT_MAP: dict[AudioFormat, str] = {
    AudioFormat.MP3: "mp3_44100_128",
    AudioFormat.WAV: "pcm_44100",
    AudioFormat.OGG: "ogg_vorbis",
    AudioFormat.PCM: "pcm_44100",
    # ElevenLabs returns raw 8-bit µ-law samples at 8 kHz with no
    # container — exactly what Telnyx Media Streams expects to
    # forward to PSTN. Don't wrap, don't resample, don't re-encode.
    AudioFormat.MULAW_8000: "ulaw_8000",
}

# Default synthesis cache capacity — enough to cover a busy day of
# recurring announcements without retaining unbounded audio in memory.
# At typical MP3 sizes (~40KB for a short phrase) this is ~10MB max.
_DEFAULT_CACHE_MAX_ENTRIES = 256

# Default cache TTL — entries expire after this many seconds. ElevenLabs
# output is deterministic for a given input, but expiring entries after
# a reasonable window bounds memory usage when lots of one-off requests
# accumulate and gives the team a path to "re-synthesize this" by
# waiting out the TTL (e.g. after changing the voice in ElevenLabs).
_DEFAULT_CACHE_TTL_SECONDS = 1800  # 30 minutes


# Cache key: everything that changes the synthesized audio bytes.
# If any of these fields differ, the backend will produce different
# output and the cache entry should not be shared.
_CacheKey = tuple[
    str,  # voice_id
    str,  # output_format value
    str,  # model_id
    str,  # text
    float | None,  # stability
    float | None,  # similarity_boost
    float,  # speed
]

# Cache value: (synthesis result, monotonic insertion timestamp) so we
# can expire entries older than the configured TTL on access.
_CacheEntry = tuple[SynthesisResult, float]


async def _open_stream_input_ws(
    *,
    voice_id: str,
    api_key: str,
    model_id: str,
    output_format: str,
) -> Any:
    """Open the ElevenLabs stream-input WebSocket. Returns the
    connected websocket. Isolated as a module-level helper so tests
    can patch it without monkey-patching ``websockets.connect``."""
    import websockets

    url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
        f"?model_id={model_id}&output_format={output_format}"
    )
    return await websockets.connect(
        url,
        additional_headers={"xi-api-key": api_key},
        max_size=None,
    )


def _alignment_to_word_events(alignment: dict[str, Any]) -> list[TTSWordTiming]:
    """Reassemble whole-word events from ElevenLabs' per-character
    alignment payload. Each whitespace-separated run of characters is
    one event; start = first char's start, end = last char's start +
    duration. Returns an empty list if the payload is malformed."""
    chars = alignment.get("chars") or []
    starts = alignment.get("charStartTimesMs") or []
    durs = alignment.get("charDurationsMs") or []
    if not (len(chars) == len(starts) == len(durs)) or not chars:
        return []
    events: list[TTSWordTiming] = []
    word_chars: list[str] = []
    word_start_ms: int | None = None
    word_end_ms: int = 0
    for ch, st, du in zip(chars, starts, durs, strict=False):
        if ch.isspace():
            if word_chars:
                events.append(TTSWordTiming(
                    word="".join(word_chars),
                    start_seconds=(word_start_ms or 0) / 1000.0,
                    end_seconds=word_end_ms / 1000.0,
                ))
                word_chars, word_start_ms = [], None
            continue
        if word_start_ms is None:
            word_start_ms = st
        word_chars.append(ch)
        word_end_ms = st + du
    if word_chars:
        events.append(TTSWordTiming(
            word="".join(word_chars),
            start_seconds=(word_start_ms or 0) / 1000.0,
            end_seconds=word_end_ms / 1000.0,
        ))
    return events


class ElevenLabsTTSStream(TTSStream):
    """Bidirectional TTS session wrapping the stream-input WebSocket.

    Frame mapping:
      - ``{"audio": "<base64>"}`` -> ``TTSAudioChunk``
      - ``{"normalizedAlignment": {...}}`` -> one ``TTSWordTiming`` per
        whitespace-delimited word reassembled from the character spans
      - ``{"isFinal": true}`` -> terminates the events iterator
      - Anything else -> ignored

    A background pump task drains ``ws.recv()`` and pushes events to an
    ``asyncio.Queue``. The ``events()`` consumer reads from that queue,
    so the producer (``send_text`` / ``flush``) never blocks on the
    consumer and vice versa.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump_recv())

    async def send_text(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("stream is closed")
        await self._ws.send(_json.dumps({"text": text}))

    async def flush(self) -> None:
        if self._closed:
            raise RuntimeError("stream is closed")
        # Per ElevenLabs docs: an empty-text frame with flush=true
        # triggers synthesis of buffered text without ending the
        # connection (that's what end-of-input ``{"text": ""}`` does).
        await self._ws.send(_json.dumps({"text": "", "flush": True}))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Polite close: signal end-of-input via an empty-string text.
        try:
            await self._ws.send(_json.dumps({"text": ""}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if not self._pump_task.done():
            self._pump_task.cancel()
        await self._events.put(None)  # sentinel — unblocks events()

    def events(self) -> AsyncIterator[Any]:
        q = self._events

        async def _gen() -> AsyncIterator[Any]:
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev

        return _gen()

    async def _pump_recv(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                try:
                    msg = _json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if "audio" in msg and msg["audio"]:
                    await self._events.put(
                        TTSAudioChunk(audio=_b64.b64decode(msg["audio"]))
                    )
                if "normalizedAlignment" in msg and msg["normalizedAlignment"]:
                    align = msg["normalizedAlignment"]
                    for word_ev in _alignment_to_word_events(align):
                        await self._events.put(word_ev)
                if msg.get("isFinal"):
                    await self._events.put(None)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Server hangup or socket error — surface as a recoverable=False
            # event and terminate the consumer cleanly.
            await self._events.put(
                TTSStreamError(message=str(e), recoverable=False)
            )
            await self._events.put(None)


class ElevenLabsTTS(TTSBackend):
    """ElevenLabs text-to-speech implementation with an in-memory LRU cache.

    Identical synthesis requests (same text, voice, format, model, and
    voice settings) are served from the cache without hitting the API.
    This is important for recurring alarms and repeated announcements —
    a 15-second wake-up alarm would otherwise burn thousands of API
    calls per day for the same short phrase.
    """

    backend_name = "elevenlabs"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="ElevenLabs API key.",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="voice_id",
                type=ToolParameterType.STRING,
                description="ElevenLabs voice ID for speech synthesis.",
                restart_required=True,
            ),
            ConfigParam(
                key="model_id",
                type=ToolParameterType.STRING,
                description="ElevenLabs model ID.",
                default="eleven_turbo_v2_5",
            ),
            ConfigParam(
                key="cache_max_entries",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum number of synthesis results to keep in the "
                    "in-memory LRU cache. Identical requests return the "
                    "cached audio without hitting the API. Set to 0 to "
                    "disable caching."
                ),
                default=_DEFAULT_CACHE_MAX_ENTRIES,
            ),
            ConfigParam(
                key="cache_ttl_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "How long a cached synthesis result stays valid (in "
                    "seconds). After this, the entry is evicted on next "
                    "access and the API is called again. Default 1800 "
                    "(30 minutes). Set to 0 to disable the TTL — entries "
                    "only age out via LRU eviction."
                ),
                default=_DEFAULT_CACHE_TTL_SECONDS,
            ),
            ConfigParam(
                key="enable_audio_tags",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Send each text through a small AI model to inject "
                    "ElevenLabs v3 audio tags ([excited], [laughs], etc.) "
                    "for more expressive delivery. Requires an "
                    "ElevenLabs v3 model; older models will speak the "
                    "tags literally. Adds one AI round-trip per unique "
                    "phrase (subsequent calls hit the tag cache)."
                ),
                default=False,
            ),
            ConfigParam(
                key="audio_tag_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI profile used for the audio-tag director call. "
                    "Pick a fast, cheap profile (e.g. one targeting a "
                    "Haiku-class model) — every tagged synthesis pays "
                    "for one round-trip with this profile. Leave blank "
                    "to use the AI service's default backend/model."
                ),
                default="",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="audio_tag_system_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the audio-tag director. Edit to "
                    "change the tag vocabulary, density, or delivery "
                    "style. Leave blank to use the built-in default."
                ),
                default=_DEFAULT_AUDIO_TAG_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="audio_tag_context_template",
                type=ToolParameterType.STRING,
                description=(
                    "Template applied when the caller passes a "
                    "``context`` on the synthesis request. Must include "
                    "``{context}`` and ``{text}`` placeholders — the "
                    "rendered string becomes the user message sent to "
                    "the director. The default system prompt expects "
                    "the ``Context: ... \\n\\nText: ...`` shape; if you "
                    "edit this, edit the system prompt to match. Leave "
                    "blank to use the built-in default."
                ),
                default=_DEFAULT_AUDIO_TAG_CONTEXT_TEMPLATE,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="audio_tag_min_chars",
                type=ToolParameterType.INTEGER,
                description=(
                    "Minimum text length (in characters) before audio "
                    "tagging kicks in. Shorter inputs are sent to the "
                    "TTS API verbatim — the latency of an extra AI call "
                    "isn't worth it for one-liners."
                ),
                default=_DEFAULT_AUDIO_TAG_MIN_CHARS,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify the ElevenLabs API key works by listing the available voices."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="ElevenLabs backend is not initialized — save settings first.",
            )
        # list_voices is a cheap authenticated GET that exercises the API
        # key without synthesizing audio or spending credits.
        try:
            voices = await self.list_voices()
        except httpx.HTTPStatusError as exc:
            reason = (
                "API key rejected (401)"
                if exc.response.status_code == 401
                else f"HTTP {exc.response.status_code}"
            )
            return ConfigActionResult(
                status="error",
                message=f"ElevenLabs API error: {reason}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to ElevenLabs ({len(voices)} voices available).",
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._api_key: str = ""
        self._voice_id: str = ""
        self._model_id: str = "eleven_turbo_v2_5"
        self._cache: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        self._cache_max_entries: int = _DEFAULT_CACHE_MAX_ENTRIES
        self._cache_ttl_seconds: float = float(_DEFAULT_CACHE_TTL_SECONDS)
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_evictions: int = 0

        # Audio-tag injection state. Disabled by default; enabled via
        # ``enable_audio_tags`` once an AI sampling provider is wired in
        # by the TTSService.
        self._enable_audio_tags: bool = False
        self._audio_tag_profile: str = ""
        self._audio_tag_system_prompt: str = _DEFAULT_AUDIO_TAG_SYSTEM_PROMPT
        self._audio_tag_context_template: str = _DEFAULT_AUDIO_TAG_CONTEXT_TEMPLATE
        self._audio_tag_min_chars: int = _DEFAULT_AUDIO_TAG_MIN_CHARS
        self._ai_sampling: AISamplingProvider | None = None
        # Cache: (text, prompt_hash) -> tagged_text. Keyed on the prompt
        # hash so editing the system prompt invalidates without us having
        # to walk the cache.
        self._tag_cache: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def initialize(self, config: dict[str, object]) -> None:
        api_key = config.get("api_key")
        if not api_key or not isinstance(api_key, str):
            raise ValueError("ElevenLabs TTS requires 'api_key' in config")
        self._api_key = api_key

        self._voice_id = str(config.get("voice_id", ""))

        if "model_id" in config:
            model_id = config["model_id"]
            if isinstance(model_id, str):
                self._model_id = model_id

        # Cache capacity (optional, falls back to default). Config values
        # come in as ``object`` from the dict, so coerce via ``str()``
        # which all the expected types (int/float/str) handle cleanly.
        cache_cap_raw = config.get("cache_max_entries")
        if cache_cap_raw is not None:
            try:
                self._cache_max_entries = max(0, int(str(cache_cap_raw)))
            except (TypeError, ValueError):
                self._cache_max_entries = _DEFAULT_CACHE_MAX_ENTRIES

        # Cache TTL (optional, 0 disables expiry but not eviction)
        cache_ttl_raw = config.get("cache_ttl_seconds")
        if cache_ttl_raw is not None:
            try:
                self._cache_ttl_seconds = max(0.0, float(str(cache_ttl_raw)))
            except (TypeError, ValueError):
                self._cache_ttl_seconds = float(_DEFAULT_CACHE_TTL_SECONDS)

        self._enable_audio_tags = bool(config.get("enable_audio_tags", False))

        tag_profile_raw = config.get("audio_tag_profile")
        if isinstance(tag_profile_raw, str):
            self._audio_tag_profile = tag_profile_raw.strip()
        else:
            self._audio_tag_profile = ""

        tag_prompt_raw = config.get("audio_tag_system_prompt")
        if isinstance(tag_prompt_raw, str) and tag_prompt_raw.strip():
            self._audio_tag_system_prompt = tag_prompt_raw
        else:
            # Empty / non-string falls back to the built-in default — we
            # never send an empty system prompt to the AI service.
            self._audio_tag_system_prompt = _DEFAULT_AUDIO_TAG_SYSTEM_PROMPT

        tag_ctx_raw = config.get("audio_tag_context_template")
        if isinstance(tag_ctx_raw, str) and tag_ctx_raw.strip():
            # The template is a Python format string; we only validate
            # it contains the required placeholders. Anything more
            # exotic is the user's responsibility.
            if "{context}" in tag_ctx_raw and "{text}" in tag_ctx_raw:
                self._audio_tag_context_template = tag_ctx_raw
            else:
                logger.warning(
                    "audio_tag_context_template missing {context} or {text} "
                    "placeholder; falling back to default"
                )
                self._audio_tag_context_template = _DEFAULT_AUDIO_TAG_CONTEXT_TEMPLATE
        else:
            self._audio_tag_context_template = _DEFAULT_AUDIO_TAG_CONTEXT_TEMPLATE

        tag_min_raw = config.get("audio_tag_min_chars")
        if tag_min_raw is not None:
            try:
                self._audio_tag_min_chars = max(0, int(str(tag_min_raw)))
            except (TypeError, ValueError):
                self._audio_tag_min_chars = _DEFAULT_AUDIO_TAG_MIN_CHARS

        self._cache.clear()
        # Drop any stale tagged-text entries on (re)init. Cheap and
        # avoids cross-config-revision bleed-through when the prompt or
        # model id changes.
        self._tag_cache.clear()

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        logger.info(
            "ElevenLabs TTS initialized (model=%s, cache_max=%d, cache_ttl=%.0fs)",
            self._model_id,
            self._cache_max_entries,
            self._cache_ttl_seconds,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        # Release cached audio so a restart starts fresh.
        self._cache.clear()
        self._tag_cache.clear()

    # --- AICapableTTSBackend protocol ---

    def set_ai_sampling(self, ai: object) -> None:
        """Receive the AI sampling provider for audio-tag injection.

        Narrowed at the boundary to keep ``set_ai_sampling`` callable
        from generic TTS-service code that doesn't import
        ``AISamplingProvider``.
        """
        if isinstance(ai, AISamplingProvider):
            self._ai_sampling = ai

    # --- Audio-tag injection ---

    async def _inject_audio_tags(self, text: str, context: str = "") -> str:
        """Return ``text`` with v3 audio tags inserted, or the original
        text if injection is disabled, not applicable, or the AI call
        fails. Never raises — failures fall back to raw text so TTS
        keeps working even if the director model is down.

        ``context`` is an optional caller-supplied description of the
        situation/mood that the director uses as a hint. When non-empty
        it's wrapped around ``text`` via
        ``audio_tag_context_template`` before being sent as the user
        message; the cache key includes the context so two calls with
        the same text but different contexts are tagged independently.
        """
        if not self._enable_audio_tags:
            return text
        if self._ai_sampling is None:
            return text
        if len(text) < self._audio_tag_min_chars:
            return text
        if _TAG_RE.search(text):
            # Caller already authored tags by hand — respect them.
            return text

        prompt_hash = hashlib.sha1(
            self._audio_tag_system_prompt.encode("utf-8")
        ).hexdigest()
        cache_key = (text, context, prompt_hash)
        cached = self._tag_cache.get(cache_key)
        if cached is not None:
            self._tag_cache.move_to_end(cache_key)
            return cached

        if context:
            try:
                user_message = self._audio_tag_context_template.format(
                    context=context, text=text
                )
            except (KeyError, IndexError):
                # Bad template (e.g. extra unfilled placeholder) —
                # skip the wrap rather than crash; the director still
                # gets the raw text and we lose only the context hint.
                logger.warning(
                    "audio_tag_context_template format failed; sending raw text",
                    exc_info=True,
                )
                user_message = text
        else:
            user_message = text

        try:
            response = await self._ai_sampling.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_message)],
                system_prompt=self._audio_tag_system_prompt,
                tools_override=[],
                profile_name=self._audio_tag_profile or None,
            )
        except Exception:
            logger.warning(
                "Audio-tag injection failed; falling back to raw text",
                exc_info=True,
            )
            return text

        tagged = (response.message.content or "").strip()
        if not tagged:
            logger.debug(
                "Audio-tag injector returned empty content; using raw text"
            )
            return text

        if not _tagged_response_overlaps_input(text, tagged):
            logger.warning(
                "Audio-tag injector response diverged from input "
                "(likely instruction-following hallucination); falling back to raw text. "
                "input=%r got=%r",
                text,
                tagged,
            )
            return text

        # One INFO line per non-cached injection so users can audit
        # which tags the director chose. Subsequent identical inputs
        # hit ``_tag_cache`` and skip this branch — keeping the log
        # quiet under repeat traffic.
        if context:
            logger.info(
                "Audio tags injected (context=%r): %r → %r", context, text, tagged
            )
        else:
            logger.info("Audio tags injected: %r → %r", text, tagged)

        self._tag_cache[cache_key] = tagged
        self._tag_cache.move_to_end(cache_key)
        while len(self._tag_cache) > _AUDIO_TAG_CACHE_MAX:
            self._tag_cache.popitem(last=False)
        return tagged

    # --- Cache ---

    def _make_cache_key(self, request: SynthesisRequest) -> _CacheKey:
        """Build a cache key from every field that affects the output audio."""
        return (
            request.voice_id,
            request.output_format.value,
            self._model_id,
            request.text,
            request.stability,
            request.similarity_boost,
            request.speed,
        )

    def _cache_get(self, key: _CacheKey) -> SynthesisResult | None:
        """LRU lookup with TTL expiry.

        Returns the stored result on hit, or None on miss or expiry.
        Expired entries are removed from the cache as a side effect
        (lazy expiration — no background sweeper needed).
        """
        if self._cache_max_entries == 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None

        result, inserted_at = entry
        if self._cache_ttl_seconds > 0:
            age = time.monotonic() - inserted_at
            if age >= self._cache_ttl_seconds:
                # Entry expired — evict and treat as a miss
                del self._cache[key]
                self._cache_evictions += 1
                return None

        # Refresh LRU order
        self._cache.move_to_end(key)
        return result

    def _cache_put(self, key: _CacheKey, result: SynthesisResult) -> None:
        """Insert with timestamp and LRU eviction at capacity."""
        if self._cache_max_entries == 0:
            return
        self._cache[key] = (result, time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)
            self._cache_evictions += 1

    def cache_stats(self) -> dict[str, Any]:
        """Snapshot of cache metrics — used by tests and observability."""
        return {
            "size": len(self._cache),
            "max_entries": self._cache_max_entries,
            "ttl_seconds": self._cache_ttl_seconds,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "evictions": self._cache_evictions,
        }

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        # Use configured voice_id as default if request doesn't specify one
        if not request.voice_id:
            if self._voice_id:
                request = SynthesisRequest(
                    text=request.text,
                    voice_id=self._voice_id,
                    output_format=request.output_format,
                    speed=request.speed,
                    stability=request.stability,
                    similarity_boost=request.similarity_boost,
                    context=request.context,
                )
            else:
                raise ValueError("No voice_id configured — set voice_id in TTS backend settings")

        # Inject audio tags up-front so the cache key reflects the
        # actual text the API will see. Two consecutive identical raw
        # inputs reuse the same tagged version (via _tag_cache) and
        # then the same audio (via _cache) — keeping cache hit rates
        # intact when tagging is on.
        tagged_text = await self._inject_audio_tags(request.text, request.context)
        if tagged_text != request.text:
            request = SynthesisRequest(
                text=tagged_text,
                voice_id=request.voice_id,
                output_format=request.output_format,
                speed=request.speed,
                stability=request.stability,
                similarity_boost=request.similarity_boost,
                context=request.context,
            )

        # Cache hit check before touching the API
        cache_key = self._make_cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            logger.debug(
                "ElevenLabs TTS cache hit for voice=%s (%d chars)",
                request.voice_id,
                len(request.text),
            )
            return cached

        self._cache_misses += 1
        client = self._require_client()

        output_format = _FORMAT_MAP.get(request.output_format, "mp3_44100_128")

        body: dict[str, Any] = {
            "text": request.text,
            "model_id": self._model_id,
        }

        voice_settings: dict[str, float] = {}
        if request.stability is not None:
            voice_settings["stability"] = request.stability
        if request.similarity_boost is not None:
            voice_settings["similarity_boost"] = request.similarity_boost
        if voice_settings:
            body["voice_settings"] = voice_settings

        response = await client.post(
            f"/text-to-speech/{request.voice_id}",
            json=body,
            params={"output_format": output_format},
        )
        response.raise_for_status()

        audio = response.content

        characters_used = len(request.text)

        result = SynthesisResult(
            audio=audio,
            format=request.output_format,
            characters_used=characters_used,
        )
        # Only cache successful synthesis
        self._cache_put(cache_key, result)
        return result

    def synthesize_stream(
        self, request: SynthesisRequest,
    ) -> AsyncIterator[bytes]:
        """Stream MP3/PCM audio chunks via the ElevenLabs streaming endpoint.

        Skips the local response cache — streaming is intended for
        long replies where the caller wants minimal first-byte latency
        anyway. Skips audio-tag injection too; the director model would
        block first-byte latency on its own round-trip. Callers that
        want tagged audio should use ``synthesize`` instead."""
        if not request.voice_id:
            if self._voice_id:
                request = SynthesisRequest(
                    text=request.text, voice_id=self._voice_id,
                    output_format=request.output_format, speed=request.speed,
                    stability=request.stability, similarity_boost=request.similarity_boost,
                    context=request.context,
                )
            else:
                raise ValueError("No voice_id configured — set voice_id in TTS backend settings")
        client = self._require_client()
        output_format = _FORMAT_MAP.get(request.output_format, "mp3_44100_128")
        body: dict[str, Any] = {"text": request.text, "model_id": self._model_id}
        voice_settings: dict[str, float] = {}
        if request.stability is not None:
            voice_settings["stability"] = request.stability
        if request.similarity_boost is not None:
            voice_settings["similarity_boost"] = request.similarity_boost
        if voice_settings:
            body["voice_settings"] = voice_settings

        async def _gen() -> AsyncIterator[bytes]:
            async with client.stream(
                "POST",
                f"/text-to-speech/{request.voice_id}/stream",
                json=body,
                params={"output_format": output_format},
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk

        return _gen()

    async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
        """Open a bidirectional TTS session via the stream-input WS API.

        The WebSocket requires a priming frame as the first send before
        any subsequent text frames are accepted; we send a single space
        with default voice_settings so callers don't have to. Subsequent
        ``send_text()`` calls drive synthesis, ``flush()`` forces
        rendering of buffered text, and ``close()`` ends the session.
        """
        voice_id = config.voice_id or self._voice_id
        if not voice_id:
            raise ValueError(
                "No voice_id configured — set voice_id in TTS backend settings"
            )
        if not self._api_key:
            raise RuntimeError(
                "ElevenLabs TTS not initialized — call initialize() first"
            )
        output_format = _FORMAT_MAP.get(config.output_format, "mp3_44100_128")
        ws = await _open_stream_input_ws(
            voice_id=voice_id,
            api_key=self._api_key,
            model_id=self._model_id,
            output_format=output_format,
        )
        # ElevenLabs requires a priming frame as the first send. We use
        # a single-space placeholder so we don't synthesize anything
        # audible — subsequent send_text frames are what actually
        # produce audio.
        await ws.send(
            _json.dumps(
                {
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
                }
            )
        )
        return ElevenLabsTTSStream(ws)

    async def list_voices(self) -> list[Voice]:
        client = self._require_client()
        response = await client.get("/voices")
        response.raise_for_status()

        data = response.json()
        voices: list[Voice] = []
        for v in data.get("voices", []):
            voices.append(
                Voice(
                    voice_id=v["voice_id"],
                    name=v.get("name", v["voice_id"]),
                    language=v.get("fine_tuning", {}).get("language"),
                    description=v.get("description"),
                    labels=v.get("labels", {}),
                )
            )
        return voices

    async def get_voice(self, voice_id: str) -> Voice | None:
        client = self._require_client()
        response = await client.get(f"/voices/{voice_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        v = response.json()
        return Voice(
            voice_id=v["voice_id"],
            name=v.get("name", v["voice_id"]),
            language=v.get("fine_tuning", {}).get("language"),
            description=v.get("description"),
            labels=v.get("labels", {}),
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ElevenLabs TTS not initialized — call initialize() first")
        return self._client
