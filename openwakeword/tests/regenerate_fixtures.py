"""Regenerate the audio fixtures for ``test_hey_gilbert_audio.py``.

Usage:

    uv run python std-plugins/openwakeword/tests/regenerate_fixtures.py

Reads the configured ElevenLabs ``api_key`` + ``voice_id`` from the local
``.gilbert/gilbert.db`` (entity ``c_gilbert.config`` row id ``tts``),
calls ElevenLabs' TTS API for the positive ("Hey Gilbert.") and negative
("Hey ballsack.") phrases at 16-bit PCM @ 16 kHz mono, and writes the
raw bytes into ``tests/fixtures/``. The test suite reads these files at
run time — there is no API call in the test path itself.

Run this script once at fixture-creation time and once whenever the
ElevenLabs voice / model changes enough to need fresh samples. Commit
the resulting ``.bin`` files alongside the test.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx

_HERE = Path(__file__).resolve().parent
_FIXTURE_DIR = _HERE / "fixtures"
_POSITIVE = _FIXTURE_DIR / "hey_gilbert.pcm16_16k.bin"
_NEGATIVE = _FIXTURE_DIR / "hey_ballsack.pcm16_16k.bin"
_GILBERT_ROOT = _HERE.parents[2]
_CONFIG_DB = _GILBERT_ROOT / ".gilbert" / "gilbert.db"


def _load_elevenlabs_config() -> tuple[str, str]:
    """Return (api_key, voice_id) from the gilbert config DB.

    Raises SystemExit with a clear message when ElevenLabs isn't
    configured — generation can't proceed without it.
    """
    if not _CONFIG_DB.exists():
        raise SystemExit(f"config DB not found at {_CONFIG_DB}")
    con = sqlite3.connect(str(_CONFIG_DB))
    try:
        row = con.execute(
            'SELECT data FROM "c_gilbert.config" WHERE id = ?', ("tts",)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("no 'tts' entry found in c_gilbert.config")
    raw = row[0]
    data = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
    settings = (data or {}).get("settings") or {}
    api_key = settings.get("api_key")
    voice_id = settings.get("voice_id")
    if not api_key or not voice_id:
        raise SystemExit(
            "ElevenLabs api_key or voice_id missing from gilbert config "
            "(set via /settings -> Media -> TTS)"
        )
    return api_key, voice_id


async def _synthesize_pcm16_16k(text: str, api_key: str, voice_id: str) -> bytes:
    """Hit ElevenLabs TTS directly, return raw 16-bit mono PCM @ 16 kHz."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    params = {"output_format": "pcm_16000"}
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    body = {"text": text, "model_id": "eleven_v3"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, params=params, headers=headers, json=body)
        resp.raise_for_status()
        return resp.content


async def _main() -> int:
    api_key, voice_id = _load_elevenlabs_config()
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [
        ("Hey Gilbert.", _POSITIVE),
        ("Hey ballsack.", _NEGATIVE),
    ]
    for text, path in pairs:
        print(f"synthesizing {text!r} -> {path}")
        audio = await _synthesize_pcm16_16k(text, api_key, voice_id)
        path.write_bytes(audio)
        print(f"  wrote {len(audio)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
