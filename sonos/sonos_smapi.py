"""SMAPI playback for Spotify on Sonos — UPnP SOAP bridge.

Why this exists: Sonos's newer aiosonos-local-WebSocket ``loadContent``
path does NOT resolve raw ``spotify:track:…`` URIs against the
speaker's linked Spotify account — the call is accepted but returns
``ERROR_COMMAND_FAILED: Failed to enqueue track``. Music Assistant
(the primary consumer of aiosonos) sidesteps this by proxying audio
through its own HTTP server; the Sonos Cloud Control API explicitly
doesn't support queuing music-service tracks either (confirmed by
Sonos staff in their own community forum).

The one path that DOES still work on current firmware is the legacy
UPnP SOAP AVTransport endpoint on port 1400. That's SoCo's share-link
approach, and the endpoint is alive in the device description of
every Sonos we've seen (verified against S16 firmware 94.1-75110).

This module is a minimal, aiosonos-compatible SOAP bridge. It does
NOT import SoCo. It hits three AVTransport actions
(``RemoveAllTracksFromQueue``, ``AddURIToQueue``, ``Play``) plus two
more for coordinator queue routing (``SetAVTransportURI``, ``Seek``),
using the DIDL-Lite envelope shape SoCo's sharelink plugin builds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

import httpx

logger = logging.getLogger(__name__)

_AV_CONTROL_PATH = "/MediaRenderer/AVTransport/Control"
_AV_NS = "urn:schemas-upnp-org:service:AVTransport:1"
_SOAP_TIMEOUT = 10.0

# SMAPI service numbers for Spotify. The "X share" class in SoCo
# historically used 2311 (world) and switched to 3079 for the US
# variant. Different households land on different numbers depending
# on which Spotify integration the Sonos installer linked. We try
# them in order until one succeeds.
_SPOTIFY_SMAPI_SERVICE_NUMBERS = (2311, 3079)

# Per-kind magic values extracted from SoCo's sharelink plugin.
# ``prefix`` goes in front of the encoded spotify URI to form the
# EnqueuedURI; ``key`` prefixes the DIDL item_id; ``item_class`` is
# the upnp:class. For track/episode the EnqueuedURI has no scheme
# prefix at all — Sonos infers the service from the DIDL ``<desc>``.
_SPOTIFY_KIND_MAGIC: dict[str, dict[str, str]] = {
    "track": {
        "prefix": "",
        "key": "00032020",
        "item_class": "object.item.audioItem.musicTrack",
    },
    "episode": {
        "prefix": "",
        "key": "00032020",
        "item_class": "object.item.audioItem.musicTrack",
    },
    "album": {
        "prefix": "x-rincon-cpcontainer:1004206c",
        "key": "00040000",
        "item_class": "object.container.album.musicAlbum",
    },
    "playlist": {
        "prefix": "x-rincon-cpcontainer:1006206c",
        "key": "1006206c",
        "item_class": "object.container.playlistContainer",
    },
    "show": {
        "prefix": "x-rincon-cpcontainer:1006206c",
        "key": "1006206c",
        "item_class": "object.container.playlistContainer",
    },
}

_DIDL_TEMPLATE = (
    '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
    ' xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
    ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
    '<item id="{item_id}" parentID="-1" restricted="true">'
    "<dc:title>{title}</dc:title>"
    "<upnp:class>{item_class}</upnp:class>"
    '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
    "SA_RINCON{sn}_X_#Svc{sn}-0-Token"
    "</desc>"
    "</item>"
    "</DIDL-Lite>"
)


@dataclass
class SmapiEnqueue:
    """Pre-built EnqueuedURI + DIDL envelope for a Spotify item.

    Built once per fire so the caller can retry against multiple
    SMAPI service numbers without rebuilding from scratch.
    """

    enqueue_uri: str
    didl_metadata: str


class SmapiError(Exception):
    """Raised when a SOAP call returns a UPnP fault or non-200."""


def build_spotify_enqueue(
    kind: str, spotify_id: str, title: str, service_number: int
) -> SmapiEnqueue:
    """Construct the EnqueuedURI + DIDL pair Sonos wants for Spotify.

    ``kind`` is ``track``/``album``/``playlist``/``artist``/etc. —
    must be a key of ``_SPOTIFY_KIND_MAGIC``.

    Matches the exact shape SoCo's sharelink plugin emits (share-tested
    on a wide range of firmware versions). The encoded URI uses the
    Sonos convention of lowercase ``%3a`` for colons; the DIDL item_id
    is the kind-specific key concatenated with that encoded URI.
    """
    if kind not in _SPOTIFY_KIND_MAGIC:
        raise ValueError(f"Unsupported Spotify kind for SMAPI playback: {kind}")
    magic = _SPOTIFY_KIND_MAGIC[kind]

    # Sonos wants the colons in ``spotify:track:<id>`` percent-encoded
    # as lowercase ``%3a`` — uppercase works too in most cases but
    # some firmware is picky, so we match SoCo's proven lowercase.
    canonical = f"spotify:{kind}:{spotify_id}"
    encoded = canonical.replace(":", "%3a")

    enqueue_uri = magic["prefix"] + encoded
    didl = _DIDL_TEMPLATE.format(
        item_id=magic["key"] + encoded,
        title=_xml_escape(title or ""),
        item_class=magic["item_class"],
        sn=service_number,
    )
    return SmapiEnqueue(enqueue_uri=enqueue_uri, didl_metadata=didl)


class SonosSmapiClient:
    """Minimal async SOAP client for AVTransport playback operations.

    One instance per ``SonosSpeaker`` backend (not per speaker IP). The
    shared ``httpx.AsyncClient`` reuses connections across every
    speaker in the household. Owned by the caller — call ``aclose()``
    on shutdown.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=_SOAP_TIMEOUT)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def play_spotify(
        self,
        coord_ip: str,
        coord_rincon_id: str,
        kind: str,
        spotify_id: str,
        title: str,
    ) -> None:
        """Play a Spotify item on the group coordinator at ``coord_ip``.

        Clears the queue, enqueues the item, points the coordinator's
        AVTransport at the local queue, seeks to the newly-added track,
        and calls ``Play``. Tries each known SMAPI service number in
        order on enqueue failure — some households are registered as
        Spotify SMAPI 2311 (world) and some as 3079 (US).

        ``coord_rincon_id`` is the RINCON-form player id (e.g.
        ``RINCON_542A1BE1908601400``); used to build the
        ``x-rincon-queue:<id>#0`` URI that points the transport at the
        coordinator's own queue.
        """
        last_exc: SmapiError | None = None
        enqueued = False
        for sn in _SPOTIFY_SMAPI_SERVICE_NUMBERS:
            pair = build_spotify_enqueue(kind, spotify_id, title, sn)
            try:
                await self._remove_all(coord_ip)
                await self._add_uri_to_queue(
                    coord_ip, pair.enqueue_uri, pair.didl_metadata
                )
                enqueued = True
                logger.info(
                    "Sonos SMAPI enqueue succeeded: coord=%s kind=%s id=%s sn=%d",
                    coord_ip,
                    kind,
                    spotify_id,
                    sn,
                )
                break
            except SmapiError as exc:
                logger.info(
                    "Sonos SMAPI enqueue with sn=%d failed: %s — "
                    "trying next service number",
                    sn,
                    exc,
                )
                last_exc = exc
        if not enqueued:
            assert last_exc is not None
            raise last_exc

        await self._set_queue_as_source(coord_ip, coord_rincon_id)
        await self._seek_track(coord_ip, 1)
        await self._play(coord_ip)

    async def resume_queue(
        self,
        coord_ip: str,
        coord_rincon_id: str,
    ) -> None:
        """Press Play on the coordinator's queue.

        Re-points AVTransport at the coordinator's own queue (idempotent
        — safe to call even when already set) then fires ``Play``. Use
        this to start/resume queue playback after a queue was built by
        successive ``enqueue_spotify`` calls, or to resume after a
        direct Spotify load left the transport pointed elsewhere.
        """
        await self._set_queue_as_source(coord_ip, coord_rincon_id)
        await self._play(coord_ip)

    async def enqueue_spotify(
        self,
        coord_ip: str,
        coord_rincon_id: str,
        kind: str,
        spotify_id: str,
        title: str,
    ) -> None:
        """Append a Spotify item to the coordinator's queue.

        Pure ``AddURIToQueue`` — nothing else. Intentionally does NOT
        call ``SetAVTransportURI`` after the add: flipping the transport
        source while a different source is playing (e.g. a direct
        Spotify load) interrupts playback and, worse, can cause the
        speaker to start playing the queue immediately. ``resume_queue``
        is the explicit entry point for "switch to the queue and play."

        The ``coord_rincon_id`` arg is no longer strictly needed (it
        fed the now-removed SetAVTransportURI call) but kept on the
        signature for symmetry with ``play_spotify`` / ``resume_queue``.

        Tries each known SMAPI service number in turn so households
        registered under different Spotify SMAPI integrations (2311
        vs. 3079) both work without user intervention.
        """
        del coord_rincon_id  # kept on signature for symmetry; unused
        last_exc: SmapiError | None = None
        for sn in _SPOTIFY_SMAPI_SERVICE_NUMBERS:
            pair = build_spotify_enqueue(kind, spotify_id, title, sn)
            try:
                await self._add_uri_to_queue(
                    coord_ip, pair.enqueue_uri, pair.didl_metadata
                )
                logger.info(
                    "Sonos SMAPI enqueue-only succeeded: coord=%s kind=%s id=%s sn=%d",
                    coord_ip,
                    kind,
                    spotify_id,
                    sn,
                )
                return
            except SmapiError as exc:
                logger.info(
                    "Sonos SMAPI enqueue-only with sn=%d failed: %s — "
                    "trying next service number",
                    sn,
                    exc,
                )
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # SOAP primitives
    # ------------------------------------------------------------------

    async def _soap(
        self,
        ip: str,
        action: str,
        arg_xml: str,
    ) -> ET.Element:
        """Issue one AVTransport SOAP call; return the parsed response root.

        Raises ``SmapiError`` on HTTP non-200 or SOAP fault. The caller
        walks the returned element tree with namespace-agnostic ``iter``
        to extract fields — AVTransport responses tend to include an
        explicit xmlns so simple string searches are fragile.
        """
        url = f"http://{ip}:1400{_AV_CONTROL_PATH}"
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="{_AV_NS}">'
            f"{arg_xml}"
            f"</u:{action}>"
            "</s:Body>"
            "</s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_AV_NS}#{action}"',
        }
        try:
            resp = await self._http.post(url, content=envelope, headers=headers)
        except httpx.HTTPError as exc:
            raise SmapiError(f"{action}: HTTP transport error: {exc}") from exc

        if resp.status_code != 200:
            # UPnP faults return 500 with a detailed <faultstring> and
            # <UPnPError>. Pull the error code if present so callers
            # can log something specific.
            detail = _extract_upnp_fault(resp.text)
            raise SmapiError(
                f"{action}: HTTP {resp.status_code} — {detail or resp.text[:200]}"
            )

        try:
            return ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise SmapiError(f"{action}: invalid XML in response: {exc}") from exc

    async def _remove_all(self, ip: str) -> None:
        await self._soap(
            ip,
            "RemoveAllTracksFromQueue",
            "<InstanceID>0</InstanceID>",
        )

    async def _add_uri_to_queue(self, ip: str, uri: str, metadata: str) -> int:
        """Enqueue at the end of the queue; returns the new track number."""
        body = (
            "<InstanceID>0</InstanceID>"
            f"<EnqueuedURI>{_xml_escape(uri)}</EnqueuedURI>"
            f"<EnqueuedURIMetaData>{_xml_escape(metadata)}</EnqueuedURIMetaData>"
            "<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>"
            "<EnqueueAsNext>0</EnqueueAsNext>"
        )
        root = await self._soap(ip, "AddURIToQueue", body)
        for elem in root.iter():
            if elem.tag.endswith("FirstTrackNumberEnqueued") and elem.text:
                try:
                    return int(elem.text)
                except ValueError:
                    break
        # Sonos always returns this field on success; missing it means
        # we got an unexpected response shape. Treat as a soft failure
        # so callers fall through the retry loop.
        raise SmapiError("AddURIToQueue: missing FirstTrackNumberEnqueued")

    async def _set_queue_as_source(self, ip: str, coord_rincon_id: str) -> None:
        """Point AVTransport at the coordinator's local queue."""
        body = (
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>x-rincon-queue:{coord_rincon_id}#0</CurrentURI>"
            "<CurrentURIMetaData></CurrentURIMetaData>"
        )
        await self._soap(ip, "SetAVTransportURI", body)

    async def _seek_track(self, ip: str, track_number: int) -> None:
        body = (
            "<InstanceID>0</InstanceID>"
            "<Unit>TRACK_NR</Unit>"
            f"<Target>{int(track_number)}</Target>"
        )
        await self._soap(ip, "Seek", body)

    async def _play(self, ip: str) -> None:
        body = "<InstanceID>0</InstanceID><Speed>1</Speed>"
        await self._soap(ip, "Play", body)


def _extract_upnp_fault(body: str) -> str:
    """Pull the UPnP errorCode + errorDescription out of a SOAP 500.

    Returns an empty string when the body isn't a parseable UPnP fault;
    callers fall back to truncated raw text in that case.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    code = ""
    desc = ""
    for elem in root.iter():
        if elem.tag.endswith("errorCode"):
            code = (elem.text or "").strip()
        elif elem.tag.endswith("errorDescription"):
            desc = (elem.text or "").strip()
    if code and desc:
        return f"UPnP {code}: {desc}"
    return code or desc
