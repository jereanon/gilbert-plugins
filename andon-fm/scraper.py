"""Now-playing scraper for Andon FM stations.

Andon Labs doesn't publish a JSON API for now-playing metadata, but the
public radio page (``/radio``) embeds the data as a JavaScript object
literal inside the HTML — same payload the web player consumes. This
module fetches the page and extracts each station's ``currentBlock``
(programming block name, description, start time, duration) by locating
the station's section by its UUID and pulling the nearest enclosing
``currentBlock:{…}`` object out with regex.

The format is brittle by nature (it's not a documented contract), so
the parser is defensive — any per-station failure leaves that station's
cache untouched and is logged at debug.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .stations import RADIO_PAGE_URL, Station

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentBlock:
    """One station's current programming block."""

    name: str = ""
    description: str = ""
    started_at: str = ""
    duration_minutes: int = 0
    image_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "started_at": self.started_at,
            "duration_minutes": self.duration_minutes,
            "image_url": self.image_url,
        }


@dataclass(frozen=True)
class NowPlayingSnapshot:
    """A single scrape result for one station."""

    station_id: str
    block: CurrentBlock
    fetched_at: float  # monotonic-ish unix seconds (set by caller)
    listeners: int = 0
    tweets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "block": self.block.to_dict(),
            "fetched_at": self.fetched_at,
            "listeners": self.listeners,
            "tweets": list(self.tweets),
        }


# Match a "currentBlock:{...}" object literal that contains balanced
# key:value pairs (no nested {}). Keys are unquoted JS identifiers,
# values are quoted strings, numbers, or ``null``.
_CURRENT_BLOCK_RE = re.compile(r'currentBlock:\{([^{}]{10,2000})\}')

# Station section markers — find the chunk of the page that describes
# one station so we can scope subsequent regex to it. The page
# concatenates station objects with no clean separator, so we slice
# from one stationId to the next.
_STATION_ID_RE = re.compile(r'stationId:"([0-9a-f-]{36})"')

# Listener count from the ``stats:{currentListeners:N,…}`` block.
_LISTENERS_RE = re.compile(r'currentListeners:(\d+)')

# Recent tweets — the page embeds ``content:"…",posted_at:"…"`` per
# tweet inside a ``replies:[]`` envelope. We pull the first few to
# show "what the station's been saying on X" alongside now-playing.
_TWEET_RE = re.compile(
    r'\{id:"(\d+)",content:"((?:[^"\\]|\\.){0,800})",posted_at:"([^"]+)"'
)


def _unescape_js_string(s: str) -> str:
    """Decode the JS-style escapes the page uses (``\\n``, ``\\"``, ``\\\\``)."""
    return (
        s.replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r'\"', '"')
        .replace(r"\'", "'")
        .replace(r"\\", "\\")
    )


def _extract_field(block_body: str, key: str) -> str:
    """Pull ``key:"value"`` from a JS-object-literal body. ``null`` → ``""``."""
    m = re.search(rf'{re.escape(key)}:"((?:[^"\\]|\\.)*?)"', block_body)
    if m:
        return _unescape_js_string(m.group(1))
    if re.search(rf'{re.escape(key)}:null', block_body):
        return ""
    return ""


def _extract_int(block_body: str, key: str) -> int:
    m = re.search(rf'{re.escape(key)}:(\d+)', block_body)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def parse_page(html: str, stations: tuple[Station, ...]) -> dict[str, NowPlayingSnapshot]:
    """Parse the Andon FM page HTML into per-station snapshots.

    Returns a mapping ``{station_id: NowPlayingSnapshot}``. Stations
    whose section can't be located (e.g. the page format changed) are
    omitted — callers should treat absence as "no data, keep prior cache".

    ``fetched_at`` is left at 0; the caller stamps it.
    """
    result: dict[str, NowPlayingSnapshot] = {}
    if not html:
        return result

    # Build an ordered list of (station_id, start_offset) so we can
    # slice each station's section from the page in source order.
    matches = list(_STATION_ID_RE.finditer(html))
    if not matches:
        logger.debug("Andon FM scraper: no stationId markers in page")
        return result

    offsets = [(m.group(1), m.start()) for m in matches]
    # Deduplicate while preserving order — the page may mention a
    # stationId multiple times (e.g. inside tweet bodies); we want the
    # first occurrence, which sits at the top of that station's record.
    seen: set[str] = set()
    unique_offsets: list[tuple[str, int]] = []
    for sid, off in offsets:
        if sid in seen:
            continue
        seen.add(sid)
        unique_offsets.append((sid, off))

    wanted_ids = {s.id for s in stations}

    for i, (sid, start) in enumerate(unique_offsets):
        if sid not in wanted_ids:
            continue
        end = unique_offsets[i + 1][1] if i + 1 < len(unique_offsets) else len(html)
        section = html[start:end]

        block_match = _CURRENT_BLOCK_RE.search(section)
        block = CurrentBlock()
        if block_match:
            body = block_match.group(1)
            block = CurrentBlock(
                name=_extract_field(body, "name"),
                description=_extract_field(body, "description"),
                started_at=_extract_field(body, "startedAt"),
                duration_minutes=_extract_int(body, "durationMinutes"),
                image_url=_extract_field(body, "imageUrl"),
            )

        listeners = 0
        lm = _LISTENERS_RE.search(section)
        if lm:
            try:
                listeners = int(lm.group(1))
            except ValueError:
                listeners = 0

        tweets: list[dict[str, Any]] = []
        for tm in _TWEET_RE.finditer(section):
            tweets.append(
                {
                    "id": tm.group(1),
                    "content": _unescape_js_string(tm.group(2)),
                    "posted_at": tm.group(3),
                }
            )
            if len(tweets) >= 3:
                break

        result[sid] = NowPlayingSnapshot(
            station_id=sid,
            block=block,
            fetched_at=0.0,
            listeners=listeners,
            tweets=tweets,
        )

    return result


async def fetch_now_playing(
    client: httpx.AsyncClient,
    stations: tuple[Station, ...],
    url: str = RADIO_PAGE_URL,
) -> dict[str, NowPlayingSnapshot]:
    """One-shot fetch + parse. Returns ``{}`` on transport or parse error."""
    try:
        # The Andon Labs CDN 403s default httpx UAs, so present as a
        # generic desktop browser (same shape any media player or
        # plain-HTTP radio device would use).
        response = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("Andon FM scraper: HTTP error %s", exc)
        return {}

    try:
        return parse_page(response.text, stations)
    except Exception as exc:  # noqa: BLE001 - defensive: page format is brittle
        logger.debug("Andon FM scraper: parse failure %s", exc)
        return {}
