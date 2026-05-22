"""Bundled Andon FM station catalog.

The four stations are pulled from the public radio page at
https://andonlabs.com/radio. The stream URLs are plain HTTP MP3
(Icecast) endpoints on Live365's CDN — the same URLs the andonlabs.com
web player and the Andon FM mobile/hardware radios tune in to.

If Andon Labs renumbers or replaces these endpoints, edit this file —
no schema migration needed, the catalog is read at import time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    """Static metadata for one Andon FM station."""

    #: UUID assigned by Andon Labs (matches the ``stationId`` on the page).
    id: str
    #: Display name (e.g. ``"Thinking Frequencies"``).
    name: str
    #: Which AI model hosts the station (free text).
    host: str
    #: Twitter / X username the station posts under.
    twitter: str
    #: Plain HTTP MP3 stream URL (Live365 CDN).
    stream_url: str
    #: Cover-art URL on Andon Labs' Supabase bucket.
    image_url: str


BUNDLED_STATIONS: tuple[Station, ...] = (
    Station(
        id="6b53fc38-ed57-4738-80d6-f9fddf981054",
        name="Thinking Frequencies",
        host="Claude",
        twitter="andon_thinking",
        stream_url="https://streaming.live365.com/a46431",
        image_url=(
            "https://viubkboawozoxznojkxw.supabase.co/storage/v1/object/public/"
            "bot-imgs/radio-stations/6b53fc38-ed57-4738-80d6-f9fddf981054.png"
        ),
    ),
    Station(
        id="df197c3e-0137-4665-95f3-0fc5cec1ee1e",
        name="OpenAIR",
        host="GPT",
        twitter="andon_openair",
        stream_url="https://streaming.live365.com/a81044",
        image_url=(
            "https://viubkboawozoxznojkxw.supabase.co/storage/v1/object/public/"
            "bot-imgs/radio-stations/df197c3e-0137-4665-95f3-0fc5cec1ee1e.png"
        ),
    ),
    Station(
        id="aab4d149-92fa-4386-9c1e-d938ecb66ee3",
        name="Backlink Broadcast",
        host="Gemini",
        twitter="andon_backlink",
        stream_url="https://streaming.live365.com/a13541",
        image_url=(
            "https://viubkboawozoxznojkxw.supabase.co/storage/v1/object/public/"
            "bot-imgs/radio-stations/aab4d149-92fa-4386-9c1e-d938ecb66ee3.png"
        ),
    ),
    Station(
        id="887ec509-2be8-433e-a27e-d05c1dc21278",
        name="Grok and Roll",
        host="Grok",
        twitter="andon_grok",
        stream_url="https://streaming.live365.com/a15419",
        image_url=(
            "https://viubkboawozoxznojkxw.supabase.co/storage/v1/object/public/"
            "bot-imgs/radio-stations/887ec509-2be8-433e-a27e-d05c1dc21278.png"
        ),
    ),
)

#: Andon FM landing page — also where the now-playing scraper reads
#: each station's currentBlock metadata.
RADIO_PAGE_URL = "https://andonlabs.com/radio"


def find_station(query: str) -> Station | None:
    """Look up a station by id, exact name, or case-insensitive substring.

    Order of preference: exact id → exact (case-insensitive) name → any
    station whose name contains ``query`` (case-insensitive) → any
    station whose host (Claude/GPT/Gemini/Grok) matches.
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None
    for s in BUNDLED_STATIONS:
        if s.id == q:
            return s
    lo = q.lower()
    for s in BUNDLED_STATIONS:
        if s.name.lower() == lo:
            return s
    for s in BUNDLED_STATIONS:
        if lo in s.name.lower():
            return s
    for s in BUNDLED_STATIONS:
        if s.host.lower() == lo:
            return s
    return None
