"""Speaker manager — outbound TTS + audio playback.

Mentra's audio output protocol is URL-based, not inline-text. The
upstream SDK builds a TTS URL (``/api/tts?text=...``) and ships it
as the ``audioUrl`` field of an ``audio_play_request`` frame; the
cloud resolves the URL against its TTS proxy (ElevenLabs) and pipes
the resulting MP3 stream to the glasses speaker.

Three calling styles:

- ``play_url(url)`` — play an arbitrary audio file from a URL the
  cloud can reach (any public MP3, WAV, etc.).
- ``speak(text)`` — server-side TTS. Builds the TTS URL with the
  text + optional voice settings and dispatches via ``play_url``.
- ``stop()`` — cancel any active playback.

Wire fields the cloud REQUIRES (omitting any of these causes
silent rejection — no error frame, no audio):

- ``packageName`` — the app's reverse-DNS id
- ``sessionId`` — current session
- ``requestId`` — uniquely identifies this play call (used to
  correlate with ``audio_play_response`` if blocking-mode is on)
- ``audioUrl`` — URL the cloud fetches
- ``volume`` — 0.0–1.0 float; default 1.0
- ``stopOtherAudio`` — bool; whether to preempt other tracks
- ``trackId`` — 0=speaker, 1=app_audio, 2=tts (TTS replies use 2)
"""

from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlencode

from ...protocol.message_types import AppToCloudMessageType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


__all__ = ["SpeakerManager"]


# Track ids — match the upstream ``TrackId`` constants. TTS goes on
# track 2 so it doesn't preempt music or app audio.
_TRACK_SPEAKER = 0
_TRACK_APP_AUDIO = 1
_TRACK_TTS = 2


class SpeakerManager:
    """Outbound audio API.

    All methods are fire-and-forget when ``stop_other_audio=False``
    (the default) — they ship the frame and return immediately. The
    cloud handles delivery to the glasses speaker asynchronously.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps

    async def play_url(
        self,
        url: str,
        *,
        volume: float = 1.0,
        track_id: int = _TRACK_SPEAKER,
        stop_other_audio: bool = False,
    ) -> None:
        """Tell the cloud to fetch + play an audio file URL.

        The URL must be reachable from Mentra Cloud (i.e. public).
        Public Gilbert URLs work — ``/api/...`` endpoints exposed
        via the tunnel are fine.
        """
        await self._deps.send_frame(
            _build_audio_play_request(
                package_name=self._deps.package_name,
                session_id=self._deps.get_session_id(),
                audio_url=url,
                volume=volume,
                track_id=track_id,
                stop_other_audio=stop_other_audio,
            )
        )

    async def speak(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        model_id: str | None = None,
        voice_settings: dict[str, float] | None = None,
        volume: float = 1.0,
        stop_other_audio: bool = False,
    ) -> None:
        """Server-side TTS. Builds the cloud's ``/api/tts`` URL with
        the text + optional voice settings, then dispatches it as a
        normal ``play_url`` call on the TTS track.

        Voice settings dict keys mirror ElevenLabs:
        ``stability`` / ``similarity_boost`` / ``style`` / ``speed``
        (all floats 0–1 except speed which is a multiplier).
        Default voice + settings: server picks.
        """
        if not text:
            return
        params: list[tuple[str, str]] = [("text", text)]
        if voice_id:
            params.append(("voice_id", voice_id))
        if model_id:
            params.append(("model_id", model_id))
        if voice_settings:
            params.append(("voice_settings", json.dumps(voice_settings)))
        tts_url = f"/api/tts?{urlencode(params)}"
        await self.play_url(
            tts_url,
            volume=volume,
            track_id=_TRACK_TTS,
            stop_other_audio=stop_other_audio,
        )

    async def stop(self, *, track_id: int | None = None) -> None:
        """Stop active playback. ``track_id=None`` stops every
        track; pass a specific id (0/1/2) to stop just one."""
        payload: dict[str, object] = {
            "type": AppToCloudMessageType.AUDIO_STOP_REQUEST.value,
            "packageName": self._deps.package_name,
            "sessionId": self._deps.get_session_id(),
            "requestId": _request_id(),
        }
        if track_id is not None:
            payload["trackId"] = int(track_id)
        await self._deps.send_frame(payload)


def _build_audio_play_request(
    *,
    package_name: str,
    session_id: str,
    audio_url: str,
    volume: float,
    track_id: int,
    stop_other_audio: bool,
) -> dict[str, object]:
    """Construct the ``audio_play_request`` frame exactly as the
    cloud expects. Missing any required field causes silent
    rejection (no error frame back, no audio played) — caught
    during the first deploy where ``text``-inline frames were
    being accepted at the WS layer but ignored by the cloud's
    audio router because the URL handoff is the only path
    actually implemented."""
    return {
        "type": AppToCloudMessageType.AUDIO_PLAY_REQUEST.value,
        "packageName": package_name,
        "sessionId": session_id,
        "requestId": _request_id(),
        "audioUrl": audio_url,
        "volume": max(0.0, min(1.0, float(volume))),
        "stopOtherAudio": bool(stop_other_audio),
        "trackId": int(track_id),
    }


def _request_id() -> str:
    return f"audio_req_{uuid.uuid4().hex[:16]}"
