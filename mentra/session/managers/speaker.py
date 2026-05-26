"""Speaker manager — outbound TTS + audio playback.

Two delivery paths the SDK supports:

1. **URL playback** — cloud fetches and renders an audio URL on
   the glasses. Simplest path; the file has to be reachable from
   Mentra Cloud (i.e. publicly addressable) and in a supported
   format (MP3 / WAV).
2. **TTS** — cloud synthesizes the text using the user's selected
   voice and plays the result.

We implement both. URL playback is preferred when Gilbert already
has a TTS pipeline (the ``elevenlabs`` / ``kokoro`` plugins) and
just needs Mentra to play the resulting MP3 — that route gives us
voice consistency across other speakers Gilbert controls.
"""

from __future__ import annotations

from ...protocol.message_types import AppToCloudMessageType
from .base import ManagerDeps


class SpeakerManager:
    """Outbound audio API."""

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps

    async def play_url(
        self,
        url: str,
        *,
        volume: float | None = None,
    ) -> None:
        """Tell the cloud to fetch + play an audio file URL.

        The URL must be reachable from Mentra Cloud's network. For
        Gilbert this typically means using ``/api/...`` URLs that
        the public tunnel (ngrok / Cloudflare tunnel) exposes.
        """
        payload: dict[str, object] = {
            "type": AppToCloudMessageType.AUDIO_PLAY_REQUEST.value,
            "packageName": self._deps.package_name,
            "sessionId": self._deps.get_session_id(),
            "audioUrl": url,
        }
        if volume is not None:
            payload["volume"] = max(0.0, min(1.0, float(volume)))
        await self._deps.send_frame(payload)

    async def speak(
        self,
        text: str,
        *,
        voice_id: str | None = None,
    ) -> None:
        """Cloud-side TTS — the cloud synthesizes ``text`` using
        whatever TTS engine is configured for the user's account.

        Use this when the operator hasn't wired Gilbert TTS to a
        URL the cloud can reach — it's the lowest-friction path
        but produces voice that's inconsistent with Gilbert's
        other speakers."""
        payload: dict[str, object] = {
            "type": AppToCloudMessageType.AUDIO_PLAY_REQUEST.value,
            "packageName": self._deps.package_name,
            "sessionId": self._deps.get_session_id(),
            "text": text,
        }
        if voice_id:
            payload["voiceId"] = voice_id
        await self._deps.send_frame(payload)

    async def stop(self) -> None:
        """Stop any currently-playing audio on this session."""
        await self._deps.send_frame(
            {
                "type": AppToCloudMessageType.AUDIO_STOP_REQUEST.value,
                "packageName": self._deps.package_name,
                "sessionId": self._deps.get_session_id(),
            }
        )
