"""Tests for the SMAPI SOAP bridge used for Spotify playback."""

from __future__ import annotations

import pytest
from gilbert_plugin_sonos.sonos_smapi import (
    SmapiError,
    SonosSmapiClient,
    build_spotify_enqueue,
)
from httpx import AsyncClient, MockTransport, Request, Response

# ── DIDL builder ─────────────────────────────────────────────────────


def test_build_spotify_track_uses_no_scheme_prefix_and_musicTrack_class() -> None:
    """A track EnqueuedURI has no scheme prefix — Sonos routes it via
    the DIDL ``<desc>`` descriptor — and the DIDL item_class must be
    ``musicTrack`` so the speaker plays it as a single item rather
    than treating it as a container (which silently does nothing).
    """
    pair = build_spotify_enqueue(
        kind="track",
        spotify_id="712uvW1Vezq8WpQi38v2L9",
        title="Bitch, Don't Kill My Vibe",
        service_number=2311,
    )

    # No scheme prefix; spotify URI is percent-encoded with lowercase
    # %3a (matches SoCo's proven shape).
    assert pair.enqueue_uri == "spotify%3atrack%3a712uvW1Vezq8WpQi38v2L9"

    # item_id uses the track key (00032020) + encoded URI.
    assert 'id="00032020spotify%3atrack%3a712uvW1Vezq8WpQi38v2L9"' in pair.didl_metadata
    assert (
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        in pair.didl_metadata
    )
    # Sonos descriptor routes the URI to Spotify SMAPI service 2311.
    assert "SA_RINCON2311_X_#Svc2311-0-Token" in pair.didl_metadata
    # Title is included verbatim in element text content (apostrophes
    # don't need escaping inside <dc:title>…</dc:title> per the XML
    # spec); but any &, <, or > would be escaped to prevent envelope
    # corruption. This specific title has none of those.
    assert "Bitch, Don't Kill My Vibe" in pair.didl_metadata


def test_build_spotify_playlist_uses_cpcontainer_prefix() -> None:
    """Playlists (and albums, artists, shows) take a
    ``x-rincon-cpcontainer:`` prefix with a kind-specific magic
    key. Without the prefix the URI would be treated as a track and
    silently no-op."""
    pair = build_spotify_enqueue(
        kind="playlist",
        spotify_id="37i9dQZF1DXcBWIGoYBM5M",
        title="Today's Top Hits",
        service_number=3079,
    )

    assert pair.enqueue_uri.startswith("x-rincon-cpcontainer:1006206c")
    assert "spotify%3aplaylist%3a37i9dQZF1DXcBWIGoYBM5M" in pair.enqueue_uri
    assert (
        "<upnp:class>object.container.playlistContainer</upnp:class>"
        in pair.didl_metadata
    )


def test_build_spotify_album_uses_album_key() -> None:
    """Albums use their own magic item_id prefix (00040000) and the
    ``musicAlbum`` class — mixing these up makes Sonos either reject
    or silently play nothing depending on which is wrong."""
    pair = build_spotify_enqueue(
        kind="album",
        spotify_id="6DEjYFkNZh67HP7R9PSZvv",
        title="DAMN.",
        service_number=2311,
    )

    assert pair.enqueue_uri.startswith("x-rincon-cpcontainer:1004206c")
    assert 'id="00040000spotify%3aalbum%3a6DEjYFkNZh67HP7R9PSZvv"' in pair.didl_metadata
    assert (
        "<upnp:class>object.container.album.musicAlbum</upnp:class>"
        in pair.didl_metadata
    )


def test_build_rejects_unsupported_kind() -> None:
    with pytest.raises(ValueError, match="Unsupported Spotify kind"):
        build_spotify_enqueue("artist", "abc", "X", 2311)


# ── End-to-end SOAP flow ─────────────────────────────────────────────


class _CallRecorder:
    """Collect every SOAP call made by the client during a play flow.

    Returns canned UPnP-shaped responses so the client sees a
    successful happy path. ``AddURIToQueue`` returns a non-empty
    ``FirstTrackNumberEnqueued`` so the client's enqueue-ok check
    passes; other actions just need to be 200 OK.
    """

    def __init__(self, fail_services: set[int] | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        # Service numbers the mock should reject on AddURIToQueue,
        # exercising the fallback loop. Empty = every service works.
        self._fail_services = fail_services or set()

    def handler(self, request: Request) -> Response:
        action = request.headers.get("SOAPAction", "").strip('"')
        body = request.content
        self.calls.append((action, body))

        if action.endswith("#AddURIToQueue"):
            # If any failing services are configured, return a SOAP
            # fault for the matching sn= in the body.
            for sn in self._fail_services:
                # DIDL descriptor uses SA_RINCON{sn}_ — search for it
                # in the raw request body.
                if f"SA_RINCON{sn}_".encode() in body:
                    fault = (
                        '<?xml version="1.0"?>'
                        "<s:Envelope>"
                        "<s:Body><s:Fault>"
                        "<detail><UPnPError>"
                        "<errorCode>800</errorCode>"
                        "<errorDescription>Invalid service</errorDescription>"
                        "</UPnPError></detail>"
                        "</s:Fault></s:Body></s:Envelope>"
                    )
                    return Response(500, text=fault)
            return Response(
                200,
                text=(
                    '<?xml version="1.0"?>'
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                    "<s:Body>"
                    "<u:AddURIToQueueResponse "
                    'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                    "<FirstTrackNumberEnqueued>1</FirstTrackNumberEnqueued>"
                    "<NumTracksAdded>1</NumTracksAdded>"
                    "<NewQueueLength>1</NewQueueLength>"
                    "</u:AddURIToQueueResponse>"
                    "</s:Body></s:Envelope>"
                ),
            )

        # Everything else: canned empty-success.
        return Response(
            200,
            text=(
                '<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                "<s:Body></s:Body></s:Envelope>"
            ),
        )


@pytest.mark.asyncio
async def test_play_spotify_end_to_end_happy_path() -> None:
    """Happy path exercises the full SOAP sequence: clear the queue,
    enqueue, point AVTransport at the coordinator's queue, seek to
    track 1, play. Each step must hit the AVTransport endpoint on
    port 1400 with the right SOAPAction header."""
    recorder = _CallRecorder()
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    await client.play_spotify(
        coord_ip="192.168.86.232",
        coord_rincon_id="RINCON_542A1BE1908601400",
        kind="track",
        spotify_id="712uvW1Vezq8WpQi38v2L9",
        title="Bitch Dont Kill My Vibe",
    )

    actions = [action for action, _ in recorder.calls]
    av_ns = "urn:schemas-upnp-org:service:AVTransport:1"
    assert actions == [
        f"{av_ns}#RemoveAllTracksFromQueue",
        f"{av_ns}#AddURIToQueue",
        f"{av_ns}#SetAVTransportURI",
        f"{av_ns}#Seek",
        f"{av_ns}#Play",
    ]

    # The SetAVTransportURI call must point the transport at the
    # coordinator's own queue via the x-rincon-queue:<id>#0 form.
    set_body = recorder.calls[2][1].decode()
    assert "x-rincon-queue:RINCON_542A1BE1908601400#0" in set_body

    # Seek targets track 1 (the freshly-enqueued track).
    seek_body = recorder.calls[3][1].decode()
    assert "<Unit>TRACK_NR</Unit>" in seek_body
    assert "<Target>1</Target>" in seek_body


@pytest.mark.asyncio
async def test_play_spotify_falls_back_to_alternate_service_number() -> None:
    """If the first SMAPI service number (2311 world) fails, the
    client must retry with 3079 (US) without aborting. Some Sonos
    households are registered against the US variant; a hard failure
    on the first attempt would make Spotify playback fail for those
    households even though it's recoverable."""
    # First attempt (sn=2311) fails; second attempt (sn=3079) succeeds.
    recorder = _CallRecorder(fail_services={2311})
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    await client.play_spotify(
        coord_ip="192.168.86.232",
        coord_rincon_id="RINCON_COORD",
        kind="track",
        spotify_id="abc",
        title="Test",
    )

    # Recorded: clear(2311) + add(2311 fails) + clear(3079) + add(3079 ok)
    # + SetAVTransportURI + Seek + Play = 7 total
    assert len(recorder.calls) == 7
    # The two enqueue attempts used different SMAPI service numbers
    # in their DIDL descriptors — confirms the fallback actually
    # rebuilt the envelope, not just retried the same one.
    add_bodies = [b for a, b in recorder.calls if a.endswith("#AddURIToQueue")]
    assert len(add_bodies) == 2
    assert b"SA_RINCON2311_" in add_bodies[0]
    assert b"SA_RINCON3079_" in add_bodies[1]


@pytest.mark.asyncio
async def test_enqueue_spotify_only_adds_does_not_touch_transport() -> None:
    """Regression guard: ``enqueue_spotify`` must be a pure
    ``AddURIToQueue``. No SetAVTransportURI, no Seek, no Play. Calling
    SetAVTransportURI mid-playback flips the transport source and can
    cause whatever else is playing to cut out / the queue to start
    playing on its own — that was the user-visible bug that prompted
    this split."""
    recorder = _CallRecorder()
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    await client.enqueue_spotify(
        coord_ip="192.168.86.232",
        coord_rincon_id="RINCON_542A1BE1908601400",
        kind="track",
        spotify_id="712uvW1Vezq8WpQi38v2L9",
        title="Black Dog",
    )

    actions = [action for action, _ in recorder.calls]
    av_ns = "urn:schemas-upnp-org:service:AVTransport:1"
    assert actions == [f"{av_ns}#AddURIToQueue"]


@pytest.mark.asyncio
async def test_resume_queue_only_sets_source_and_plays() -> None:
    """``resume_queue`` just re-points the transport at the queue and
    presses Play — no AddURIToQueue, no Seek. Used to start/resume
    queue playback after queue items were loaded separately."""
    recorder = _CallRecorder()
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    await client.resume_queue(
        coord_ip="192.168.86.232",
        coord_rincon_id="RINCON_542A1BE1908601400",
    )

    actions = [action for action, _ in recorder.calls]
    av_ns = "urn:schemas-upnp-org:service:AVTransport:1"
    assert actions == [
        f"{av_ns}#SetAVTransportURI",
        f"{av_ns}#Play",
    ]
    set_body = recorder.calls[0][1].decode()
    assert "x-rincon-queue:RINCON_542A1BE1908601400#0" in set_body


@pytest.mark.asyncio
async def test_enqueue_spotify_falls_back_to_alternate_service_number() -> None:
    """Same fallback semantics as ``play_spotify``: households registered
    under the US SMAPI variant should succeed on the second attempt."""
    recorder = _CallRecorder(fail_services={2311})
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    await client.enqueue_spotify(
        coord_ip="192.168.86.232",
        coord_rincon_id="RINCON_COORD",
        kind="track",
        spotify_id="abc",
        title="Test",
    )

    add_bodies = [b for a, b in recorder.calls if a.endswith("#AddURIToQueue")]
    assert len(add_bodies) == 2
    assert b"SA_RINCON2311_" in add_bodies[0]
    assert b"SA_RINCON3079_" in add_bodies[1]


@pytest.mark.asyncio
async def test_play_spotify_raises_when_all_service_numbers_fail() -> None:
    """If every SMAPI service number rejects the enqueue, surface the
    failure to the caller instead of silently pretending playback
    succeeded. The SonosSpeaker backend relies on the raise to emit
    its error log — a swallowed failure would leave the user with
    silent speakers and no diagnostic."""
    recorder = _CallRecorder(fail_services={2311, 3079})
    http = AsyncClient(transport=MockTransport(recorder.handler), timeout=5.0)
    client = SonosSmapiClient(http_client=http)

    with pytest.raises(SmapiError):
        await client.play_spotify(
            coord_ip="192.168.86.232",
            coord_rincon_id="RINCON_COORD",
            kind="track",
            spotify_id="abc",
            title="Test",
        )
