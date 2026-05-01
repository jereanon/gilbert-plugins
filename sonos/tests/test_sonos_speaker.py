"""Tests for the aiosonos-backed Sonos speaker backend.

The backend is a fairly thin adapter over aiosonos's WebSocket API —
most of its job is wiring up zeroconf discovery, keeping a client per
discovered player, and mapping between our SpeakerBackend interface and
aiosonos's SonosPlayer / SonosGroup / audio_clip surfaces.

Tests mock the aiosonos client/player/group objects. We don't spin up
a real WebSocket; the behaviours we care about are:

- Spotify URI / open.spotify.com URL detection.
- ``announce=True`` routes to ``player.play_audio_clip``.
- Plain HTTP URIs route to ``group.play_stream_url``.
- Spotify URIs raise NotImplementedError (deferred to the music backend
  so ``accountId`` resolution lives in one place).
- ``_ensure_group`` uses declarative grouping and is a no-op when
  membership already matches.
- Snapshot/restore are no-ops (aiosonos ``audio_clip`` self-restores).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert_plugin_sonos.sonos_speaker import (
    SonosSpeaker,
    _extract_spotify_ref,
    _PlayerMetadata,
)

from gilbert.interfaces.speaker import PlayRequest


# ── Spotify URI parsing ──────────────────────────────────────────────


def test_extract_spotify_track_uri() -> None:
    ref = _extract_spotify_ref("spotify:track:3w0pyHgJJW9JN0cJxmi33Z")
    assert ref is not None
    assert ref.kind == "track"
    assert ref.id == "3w0pyHgJJW9JN0cJxmi33Z"
    assert ref.uri == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"


def test_extract_spotify_open_url() -> None:
    """``https://open.spotify.com/playlist/…`` web URLs get canonicalized
    to ``spotify:playlist:…`` so downstream handling doesn't need two
    code paths."""
    ref = _extract_spotify_ref(
        "https://open.spotify.com/playlist/37i9dQZF1DX?si=abc"
    )
    assert ref is not None
    assert ref.kind == "playlist"
    assert ref.uri == "spotify:playlist:37i9dQZF1DX"


def test_extract_spotify_returns_none_for_plain_http() -> None:
    assert (
        _extract_spotify_ref("http://192.168.1.20:8000/api/share/abc") is None
    )


def test_extract_spotify_returns_none_for_empty() -> None:
    assert _extract_spotify_ref("") is None
    assert _extract_spotify_ref("   ") is None


# ── Test scaffolding ─────────────────────────────────────────────────


def _make_backend_with_mock_speaker(
    player_id: str = "RINCON_COORD",
    group_in: MagicMock | None = None,
) -> tuple[SonosSpeaker, MagicMock, MagicMock, MagicMock]:
    """Spin up a SonosSpeaker with one mock aiosonos client.

    Returns ``(backend, client_mock, player_mock, group_mock)``.
    """
    backend = SonosSpeaker()

    # When ``group_in`` is provided the caller has already pre-seeded
    # the fields it cares about; only fill in the ones they missed.
    # For a fresh MagicMock we always overwrite identity fields because
    # ``hasattr(MagicMock, x)`` is unconditionally True (every attr
    # returns a child MagicMock), which makes guarded fallbacks silently
    # leave MagicMock objects where the retry path expects real strings.
    fresh = group_in is None
    group = group_in if group_in is not None else MagicMock()
    if fresh or not isinstance(getattr(group, "id", None), str):
        group.id = "group-1"
    if fresh or not isinstance(getattr(group, "name", None), str):
        group.name = "Kitchen"
    if fresh or not isinstance(getattr(group, "player_ids", None), list):
        group.player_ids = [player_id]
    if fresh or not isinstance(getattr(group, "coordinator_id", None), str):
        group.coordinator_id = player_id
    if fresh or not isinstance(getattr(group, "playback_state", None), str):
        group.playback_state = "PLAYBACK_STATE_IDLE"
    if fresh:
        group.playback_metadata = None
    if not isinstance(getattr(group, "play_stream_url", None), AsyncMock):
        group.play_stream_url = AsyncMock()
    if not isinstance(getattr(group, "pause", None), AsyncMock):
        group.pause = AsyncMock()
    if not isinstance(getattr(group, "set_group_members", None), AsyncMock):
        group.set_group_members = AsyncMock()

    player = MagicMock()
    player.id = player_id
    player.name = "Kitchen"
    player.volume_level = 30
    player.is_coordinator = True
    player.group = group
    player.play_audio_clip = AsyncMock()
    player.set_volume = AsyncMock()
    player.leave_group = AsyncMock()

    client = MagicMock()
    client.player = player
    # Identity fields used by the topology-retry path — without these
    # the retry logic sees MagicMock objects where it expects strings
    # and never finds the player in the mock groups list.
    client.player_id = player_id
    client.household_id = "HH-TEST"
    client.groups = [group]
    client.create_group = AsyncMock()
    client.disconnect = AsyncMock()
    # The dispatch path hits the low-level api namespaces directly:
    # - ``api.groups.modify_group_members`` / ``api.groups.create_group``
    #   return a ``GroupInfo`` dict whose ``group.id`` is the
    #   authoritative new group id (bypasses the cache race that
    #   motivated the rewrite).
    # - ``api.playback_session.create_session`` + ``load_stream_url``
    #   replace the ``SonosGroup.play_stream_url`` wrapper so we don't
    #   read ``self.id`` from a cached (potentially stale) group.
    client.api = MagicMock()
    client.api.groups = MagicMock()
    client.api.groups.get_groups = AsyncMock()
    client.api.groups.modify_group_members = AsyncMock(
        return_value={
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": group.id,
                "name": group.name,
                "coordinatorId": player_id,
                "playerIds": list(group.player_ids),
            },
        }
    )
    client.api.groups.create_group = AsyncMock(
        return_value={
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": group.id,
                "name": group.name,
                "coordinatorId": player_id,
                "playerIds": list(group.player_ids),
            },
        }
    )
    client.api.playback_session = MagicMock()
    client.api.playback_session.create_session = AsyncMock(
        return_value={"sessionId": "session-1"}
    )
    client.api.playback_session.load_stream_url = AsyncMock()
    client.api.playback = MagicMock()
    client.api.playback.load_content = AsyncMock()
    client.api.playback.set_play_modes = AsyncMock()
    # subscribe() needs to return an unsubscribe callable for
    # _wait_for_group's subscription teardown path.
    client.subscribe = MagicMock(return_value=lambda: None)

    backend._clients[player_id] = client
    backend._player_metadata[player_id] = _PlayerMetadata(
        player_id=player_id,
        household_id="HH-TEST",
        name="Kitchen",
        ip_address="192.168.1.20",
        model="Sonos One",
    )
    return backend, client, player, group


# ── Announce routes to audio_clip ────────────────────────────────────


async def test_announce_uses_audio_clip() -> None:
    """``PlayRequest(announce=True)`` should hand the URL straight to
    ``player.play_audio_clip`` — not go through the group-forming /
    stream-loading path, and not require any snapshot/restore."""
    backend, _client, player, group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/api/share/abc.mp3",
            speaker_ids=["RINCON_COORD"],
            announce=True,
            title="Ding",
            volume=40,
        )
    )

    player.play_audio_clip.assert_awaited_once()
    assert (
        player.play_audio_clip.call_args.args[0]
        == "http://gilbert/api/share/abc.mp3"
    )
    assert player.play_audio_clip.call_args.kwargs.get("volume") == 40
    assert player.play_audio_clip.call_args.kwargs.get("name") == "Ding"

    group.play_stream_url.assert_not_awaited()
    group.set_group_members.assert_not_awaited()


async def test_announce_with_multiple_speakers_parallelizes() -> None:
    backend, _, player_a, _ = _make_backend_with_mock_speaker(
        player_id="RINCON_A"
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    client_b = MagicMock()
    player_b = MagicMock()
    player_b.play_audio_clip = AsyncMock()
    client_b.player = player_b
    backend._clients["RINCON_B"] = client_b

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/x.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
            announce=True,
        )
    )

    player_a.play_audio_clip.assert_awaited_once()
    player_b.play_audio_clip.assert_awaited_once()


# ── Plain HTTP URIs route to play_stream_url ─────────────────────────


async def test_http_uri_uses_playback_session_api() -> None:
    """HTTP(S) stream URLs go through the low-level playback_session API
    with the authoritative group id Sonos returned from the preceding
    ``modifyGroupMembers`` call — NOT through ``SonosGroup.play_stream_url``,
    which reads the group id from a cached object that can go stale."""
    backend, client, _player, _group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/api/share/song.mp3",
            speaker_ids=["RINCON_COORD"],
        )
    )

    client.api.playback_session.create_session.assert_awaited_once()
    create_kwargs = client.api.playback_session.create_session.call_args.kwargs
    create_args = client.api.playback_session.create_session.call_args.args
    # create_session(group_id, app_id=..., app_context=...) — first
    # positional arg is the authoritative group id.
    assert create_args[0] == "group-1"
    assert create_kwargs.get("app_id")
    assert create_kwargs.get("app_context") == "1"

    client.api.playback_session.load_stream_url.assert_awaited_once()
    load_kwargs = client.api.playback_session.load_stream_url.call_args.kwargs
    assert load_kwargs["session_id"] == "session-1"
    assert load_kwargs["stream_url"] == "http://gilbert/api/share/song.mp3"
    assert load_kwargs["play_on_completion"] is True
    metadata = load_kwargs["station_metadata"]
    assert metadata["_objectType"] == "container"
    assert metadata["type"] == "station"
    assert isinstance(metadata["name"], str) and metadata["name"]


async def test_play_retries_on_group_coordinator_changed() -> None:
    """Even though we use the authoritative group id from Sonos's
    ``modifyGroupMembers`` response, another controller (Sonos mobile
    app, a family member) can still reshuffle between our modify and
    our ``create_session`` call. On that race we re-run the full
    dispatch — including a fresh ``_ensure_group`` — so the next
    attempt gets a freshly-captured authoritative id."""
    from aiosonos.exceptions import FailedCommand

    backend, client, _player, _group = _make_backend_with_mock_speaker()
    call_count = {"n": 0}

    async def flaky_create_session(group_id: str, **_kwargs: object) -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise FailedCommand("Command failed: groupCoordinatorChanged")
        return {"sessionId": "session-2"}

    client.api.playback_session.create_session = (
        flaky_create_session  # type: ignore[assignment]
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_COORD"],
        )
    )

    assert call_count["n"] == 2


async def test_play_reraises_unrelated_failed_command() -> None:
    """Only ``groupCoordinatorChanged`` is retried — any other
    ``FailedCommand`` reason is a real error and should surface
    immediately."""
    from aiosonos.exceptions import FailedCommand

    backend, client, _player, _group = _make_backend_with_mock_speaker()
    client.api.playback_session.create_session = AsyncMock(
        side_effect=FailedCommand("Command failed: something else entirely")
    )

    with pytest.raises(FailedCommand, match="something else"):
        await backend.play_uri(
            PlayRequest(
                uri="http://gilbert/song.mp3",
                speaker_ids=["RINCON_COORD"],
            )
        )
    client.api.playback_session.create_session.assert_awaited_once()


async def test_play_eventually_raises_when_topology_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Sonos keeps returning ``groupCoordinatorChanged`` across all
    retries (topology never settles), surface the last error rather
    than hanging forever."""
    import asyncio as _asyncio

    from aiosonos.exceptions import FailedCommand

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    backend, client, _player, _group = _make_backend_with_mock_speaker()
    call_count = {"n": 0}

    async def always_fail(_group_id: str, **_kwargs: object) -> dict:
        call_count["n"] += 1
        raise FailedCommand("Command failed: groupCoordinatorChanged")

    client.api.playback_session.create_session = (
        always_fail  # type: ignore[assignment]
    )

    with pytest.raises(FailedCommand, match="groupCoordinatorChanged"):
        await backend.play_uri(
            PlayRequest(
                uri="http://gilbert/song.mp3",
                speaker_ids=["RINCON_COORD"],
            )
        )
    # 1 initial + 2 retries = 3 dispatch attempts. We keep the retry
    # short because with the authoritative-id design each retry is
    # wall-clock expensive (it re-runs modify_group_members) and the
    # race it defends against is a cross-controller reshuffle, which
    # shouldn't benefit from many retries in quick succession.
    assert call_count["n"] == 3


async def test_http_uri_applies_volume_before_play() -> None:
    backend, client, player, _group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_COORD"],
            volume=55,
        )
    )

    player.set_volume.assert_awaited_once_with(55)
    client.api.playback_session.create_session.assert_awaited_once()


async def test_http_uri_clamps_volume_to_valid_range() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_COORD"],
            volume=150,
        )
    )

    player.set_volume.assert_awaited_once_with(100)


# ── Spotify URIs route through the SMAPI SOAP bridge ─────────────────


async def test_spotify_uri_uses_smapi_bridge() -> None:
    """Spotify URIs go through the SMAPI SOAP bridge, not aiosonos
    ``loadContent`` — the latter is a dead API path on current S2
    firmware (see ``sonos_smapi`` for the full rationale). The bridge
    is called with the coordinator's cached IP and RINCON id plus the
    parsed kind/id/title."""
    backend, client, _player, _group = _make_backend_with_mock_speaker()
    smapi = AsyncMock()
    backend._smapi = smapi

    await backend.play_uri(
        PlayRequest(
            uri="spotify:track:3w0pyHgJJW9JN0cJxmi33Z",
            speaker_ids=["RINCON_COORD"],
            title="Bitch Dont Kill My Vibe",
        )
    )

    smapi.play_spotify.assert_awaited_once_with(
        coord_ip="192.168.1.20",
        coord_rincon_id="RINCON_COORD",
        kind="track",
        spotify_id="3w0pyHgJJW9JN0cJxmi33Z",
        title="Bitch Dont Kill My Vibe",
    )
    # Dead API path must not be touched — if it ever is, this test
    # catches it immediately rather than producing mystery "Failed to
    # enqueue track" errors at runtime.
    client.api.playback.load_content.assert_not_awaited()
    client.api.playback_session.load_stream_url.assert_not_awaited()


async def test_spotify_playlist_uses_smapi_bridge() -> None:
    """Playlist kind flows through the SMAPI bridge the same way
    tracks do — the bridge internally picks the right DIDL item_class
    and ``x-rincon-cpcontainer`` prefix."""
    backend, _client, _player, _group = _make_backend_with_mock_speaker()
    smapi = AsyncMock()
    backend._smapi = smapi

    await backend.play_uri(
        PlayRequest(
            uri="spotify:playlist:37i9dQZF1DX",
            speaker_ids=["RINCON_COORD"],
        )
    )

    smapi.play_spotify.assert_awaited_once()
    call = smapi.play_spotify.call_args
    assert call.kwargs["kind"] == "playlist"
    assert call.kwargs["spotify_id"] == "37i9dQZF1DX"


# ── get_now_playing pulls real fields out of MetadataStatus ──────────


async def test_get_now_playing_extracts_metadata_from_dict() -> None:
    """``MetadataStatus`` is a TypedDict — at runtime it's a plain dict,
    so every field access must use dict.get(), not getattr(). A prior
    getattr()-based implementation silently returned empty strings for
    every track because dicts don't expose keys as attributes. This
    test locks in dict-access by handing the code a metadata dict
    shaped exactly like aiosonos produces one and asserting the
    populated fields flow through."""
    backend, client, _player, group = _make_backend_with_mock_speaker()

    group.playback_state = "PLAYBACK_STATE_PLAYING"
    group.playback_metadata = {
        "_objectType": "metadataStatus",
        "positionMillis": 42_000,
        "currentItem": {
            "_objectType": "queueItem",
            "track": {
                "_objectType": "track",
                "type": "track",
                "name": "Bitch, Don't Kill My Vibe",
                "durationMillis": 310_000,
                "artist": {"_objectType": "artist", "name": "Kendrick Lamar"},
                "album": {"_objectType": "album", "name": "good kid, m.A.A.d city"},
                "images": [{"url": "https://img.example/kendrick.jpg"}],
            },
        },
    }

    np = await backend.get_now_playing("RINCON_COORD")
    assert np.title == "Bitch, Don't Kill My Vibe"
    assert np.artist == "Kendrick Lamar"
    assert np.album == "good kid, m.A.A.d city"
    assert np.album_art_url == "https://img.example/kendrick.jpg"
    assert np.duration_seconds == pytest.approx(310.0)
    assert np.position_seconds == pytest.approx(42.0)


async def test_get_now_playing_empty_when_no_metadata() -> None:
    """Idle speaker: aiosonos leaves playback_metadata as an empty
    TypedDict (or None). Either must return a NowPlaying with empty
    strings rather than raising — callers rely on ``NowPlaying`` being
    a safe default they can always render."""
    backend, _client, _player, group = _make_backend_with_mock_speaker()
    group.playback_metadata = {}
    group.playback_state = "PLAYBACK_STATE_IDLE"

    np = await backend.get_now_playing("RINCON_COORD")
    assert np.title == ""
    assert np.artist == ""
    assert np.album == ""


async def test_get_now_playing_falls_back_to_stream_info() -> None:
    """Radio (audioBroadcast) typically lacks ``currentItem.track`` but
    ships a ``streamInfo`` string with the station's "now playing" text
    plus a ``container`` describing the station. Without the fallback
    ladder we'd report ``state=playing`` with everything else blank —
    the exact bug the user reported with KCRW-style sources."""
    backend, _client, _player, group = _make_backend_with_mock_speaker()
    group.playback_state = "PLAYBACK_STATE_PLAYING"
    group.playback_metadata = {
        "_objectType": "metadataStatus",
        "streamInfo": "Bowie - Heroes",
        "container": {
            "_objectType": "container",
            "type": "audioBroadcast",
            "name": "KCRW",
            "imageUrl": "https://img.example/kcrw.jpg",
        },
    }

    np = await backend.get_now_playing("RINCON_COORD")
    assert np.title == "Bowie - Heroes"
    assert np.album == "KCRW"
    assert np.album_art_url == "https://img.example/kcrw.jpg"
    assert np.source == "audioBroadcast"


async def test_get_now_playing_falls_back_to_container_name() -> None:
    """Line-in / TV / AirPlay sources have no ``track`` and no
    ``streamInfo`` but always have a ``container.name`` — surface that
    as the title so the AI can say "playing from line-in" instead of
    pretending nothing's playing."""
    backend, _client, _player, group = _make_backend_with_mock_speaker()
    group.playback_state = "PLAYBACK_STATE_PLAYING"
    group.playback_metadata = {
        "_objectType": "metadataStatus",
        "container": {
            "_objectType": "container",
            "type": "linein",
            "name": "Line-In",
        },
    }

    np = await backend.get_now_playing("RINCON_COORD")
    assert np.title == "Line-In"
    assert np.artist == ""
    assert np.album == ""
    assert np.source == "linein"


async def test_get_now_playing_routes_through_coordinator_when_member_meta_empty() -> None:
    """When the queried speaker is a non-coordinator member of a group,
    aiosonos's per-player WebSocket gets a ``groupCoordinatorChanged``
    error trying to subscribe to the group's metadata, so it stores
    ``playback_metadata = {}`` on the member's view of the group. The
    coordinator's own client has the populated metadata. Without the
    coordinator-routing fallback, querying the member returns
    ``state=playing`` with everything blank — which is the bug the user
    saw on a real two-speaker setup.
    """
    # Member speaker — empty metadata (the bug condition).
    backend, member_client, _player_m, member_group = _make_backend_with_mock_speaker(
        player_id="RINCON_MEMBER"
    )
    member_group.playback_metadata = {}
    member_group.coordinator_id = "RINCON_COORD"
    member_group.playback_state = "PLAYBACK_STATE_PLAYING"

    # Coordinator's view of the same group — populated metadata.
    coord_group = MagicMock()
    coord_group.id = member_group.id
    coord_group.coordinator_id = "RINCON_COORD"
    coord_group.playback_state = "PLAYBACK_STATE_PLAYING"
    coord_group.playback_metadata = {
        "_objectType": "metadataStatus",
        "currentItem": {
            "track": {
                "name": "Hotel California",
                "artist": {"name": "Eagles"},
                "album": {"name": "Hotel California"},
                "images": [{"url": "https://img.example/hc.jpg"}],
                "durationMillis": 391_000,
            },
        },
        "container": {"type": "playlist", "name": "Classic Rock"},
        "positionMillis": 60_000,
    }
    coord_player = MagicMock()
    coord_player.id = "RINCON_COORD"
    coord_player.group = coord_group
    coord_client = MagicMock()
    coord_client.player = coord_player
    backend._clients["RINCON_COORD"] = coord_client

    np = await backend.get_now_playing("RINCON_MEMBER")
    assert np.title == "Hotel California"
    assert np.artist == "Eagles"
    assert np.album == "Hotel California"
    assert np.album_art_url == "https://img.example/hc.jpg"
    assert np.source == "playlist"


async def test_get_now_playing_track_metadata_wins_over_container() -> None:
    """When both per-track and container metadata are present (a normal
    Spotify queue play), the track-level fields take precedence —
    container is only a fallback. Album art also stays from the track,
    not the container's playlist art."""
    backend, _client, _player, group = _make_backend_with_mock_speaker()
    group.playback_state = "PLAYBACK_STATE_PLAYING"
    group.playback_metadata = {
        "_objectType": "metadataStatus",
        "currentItem": {
            "track": {
                "name": "All Falls Down",
                "artist": {"name": "Kanye"},
                "album": {"name": "College Dropout"},
                "images": [{"url": "https://img.example/cd.jpg"}],
                "durationMillis": 220_000,
            },
        },
        "container": {
            "type": "playlist",
            "name": "My Mix Vol. 3",
            "imageUrl": "https://img.example/playlist.jpg",
        },
    }

    np = await backend.get_now_playing("RINCON_COORD")
    assert np.title == "All Falls Down"
    assert np.artist == "Kanye"
    assert np.album == "College Dropout"
    assert np.album_art_url == "https://img.example/cd.jpg"
    # ``source`` still reflects the container even when the track wins —
    # callers that care about "where is this coming from" still get it.
    assert np.source == "playlist"


# ── Declarative grouping ─────────────────────────────────────────────


async def test_ensure_group_noop_when_membership_matches() -> None:
    """If the coordinator's group already contains exactly the target
    members, ``modify_group_members`` shouldn't be called — avoiding an
    unnecessary WebSocket round-trip that would briefly drop playback."""
    group = MagicMock()
    group.id = "group-1"
    group.name = "Zone"
    group.player_ids = ["RINCON_A", "RINCON_B"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, client, _player, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_player = MagicMock()
    b_player.group = group
    b_client = MagicMock()
    b_client.player = b_player
    b_client.groups = [group]
    backend._clients["RINCON_B"] = b_client

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    client.api.groups.modify_group_members.assert_not_awaited()
    client.api.playback_session.create_session.assert_awaited_once()


async def test_ensure_group_reforms_when_membership_differs() -> None:
    """If the target set doesn't match the coordinator's current group,
    ``modify_group_members`` gets called once with the diff of players
    to add and remove. We prefer ``modifyGroupMembers`` over
    ``setGroupMembers`` because the latter empirically triggers more
    aggressive coordinator election (the MA lesson learned)."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    b_client.groups = [group]
    backend._clients["RINCON_B"] = b_client

    # Simulate the server applying the group change — after modify,
    # the group's player_ids should reflect the request. Real aiosonos
    # does this via a push event; here we just mutate the mock's
    # ``player_ids`` in the side_effect so ``_wait_for_group``'s
    # predicate matches on its initial check.
    async def simulated_modify(
        group_id: str,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> dict:
        new_members = [
            *(p for p in group.player_ids if p not in player_ids_to_remove),
            *player_ids_to_add,
        ]
        group.player_ids = new_members
        return {
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": "group-1",
                "name": "Zone",
                "coordinatorId": "RINCON_A",
                "playerIds": new_members,
            },
        }

    client.api.groups.modify_group_members = AsyncMock(
        side_effect=simulated_modify
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    client.api.groups.modify_group_members.assert_awaited_once()
    call_kwargs = client.api.groups.modify_group_members.call_args.kwargs
    call_args = client.api.groups.modify_group_members.call_args.args
    # First positional is the current group id; add/remove come as kwargs.
    assert call_args[0] == "group-1"
    assert call_kwargs["player_ids_to_add"] == ["RINCON_B"]
    assert call_kwargs["player_ids_to_remove"] == []


# ── Snapshot/restore are no-ops ──────────────────────────────────────


async def test_snapshot_and_restore_are_noops() -> None:
    """Snapshot/restore are kept on the interface for backward compat
    but aiosonos's ``audio_clip`` self-restores — the new backend
    doesn't need them. Just verify they don't raise and don't poke the
    client's mutating methods."""
    backend, _client, player, group = _make_backend_with_mock_speaker()
    await backend.snapshot(["RINCON_COORD"])
    await backend.restore(["RINCON_COORD"])
    player.set_volume.assert_not_called()
    group.play_stream_url.assert_not_called()


# ── Volume ───────────────────────────────────────────────────────────


async def test_set_volume() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()
    await backend.set_volume("RINCON_COORD", 42)
    player.set_volume.assert_awaited_once_with(42)


async def test_set_volume_unknown_speaker_raises() -> None:
    backend, *_ = _make_backend_with_mock_speaker()
    with pytest.raises(KeyError, match="Unknown speaker"):
        await backend.set_volume("RINCON_NOPE", 42)


async def test_get_volume() -> None:
    backend, _client, player, _group = _make_backend_with_mock_speaker()
    player.volume_level = 73
    assert await backend.get_volume("RINCON_COORD") == 73


# ── Stop ─────────────────────────────────────────────────────────────


async def test_stop_dedupes_across_group_members() -> None:
    """When multiple speakers share a group, ``stop`` pauses the group
    once — pausing the same group N times is wasteful and can race."""
    group = MagicMock()
    group.id = "group-1"
    group.pause = AsyncMock()

    backend, _c, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A", group_in=group
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    backend._clients["RINCON_B"] = b_client

    await backend.stop(["RINCON_A", "RINCON_B"])

    group.pause.assert_awaited_once()


# ── Feature flags ────────────────────────────────────────────────────


def test_supports_grouping_is_true() -> None:
    assert SonosSpeaker().supports_grouping is True


def test_supports_repeat_is_true() -> None:
    """Sonos has native queue repeat-mode support — gates the
    ``/music loop`` tool registration in MusicService."""
    assert SonosSpeaker.supports_repeat is True


def test_backend_name() -> None:
    assert SonosSpeaker.backend_name == "sonos"


# ── Repeat / loop mode ────────────────────────────────────────────────


async def test_set_repeat_track_calls_set_play_modes_with_repeat_one() -> None:
    """LoopMode.TRACK → setPlayModes(repeat=False, repeat_one=True).
    Routes through the group coordinator's WebSocket since
    ``setPlayModes`` is a group-scoped command on the local API."""
    from gilbert.interfaces.speaker import LoopMode as _LoopMode

    backend, client, _player, _group = _make_backend_with_mock_speaker()

    await backend.set_repeat(_LoopMode.TRACK, ["RINCON_COORD"])

    client.api.playback.set_play_modes.assert_awaited_once()
    kwargs = client.api.playback.set_play_modes.call_args.kwargs
    assert kwargs["repeat"] is False
    assert kwargs["repeat_one"] is True


async def test_set_repeat_off_clears_both_flags() -> None:
    """LoopMode.OFF must explicitly clear BOTH flags so a subsequent
    'turn off repeat' actually disables whichever mode was on."""
    from gilbert.interfaces.speaker import LoopMode as _LoopMode

    backend, client, _player, _group = _make_backend_with_mock_speaker()

    await backend.set_repeat(_LoopMode.OFF, ["RINCON_COORD"])

    kwargs = client.api.playback.set_play_modes.call_args.kwargs
    assert kwargs["repeat"] is False
    assert kwargs["repeat_one"] is False


async def test_set_repeat_dedupes_across_group_members() -> None:
    """Two speakers in the same group share one queue — the play-mode
    command should fire ONCE per group, not once per member."""
    from gilbert.interfaces.speaker import LoopMode as _LoopMode

    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A", "RINCON_B"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, _client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A", group_in=group
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    b_client.api = MagicMock()
    b_client.api.playback = MagicMock()
    b_client.api.playback.set_play_modes = AsyncMock()
    backend._clients["RINCON_B"] = b_client

    await backend.set_repeat(_LoopMode.ALL, ["RINCON_A", "RINCON_B"])

    # Coordinator client gets the call; B's client should not be hit
    # twice for the same group.
    coord_client = backend._clients["RINCON_A"]
    coord_client.api.playback.set_play_modes.assert_awaited_once()


# ── Duplicate player_id handling ─────────────────────────────────────


async def test_ensure_group_waits_for_membership_to_settle() -> None:
    """``_ensure_group`` must verify that all requested speakers have
    actually joined the group before returning — otherwise callers
    can start streaming before stragglers are audible. Simulate Sonos
    accepting the modify but only landing ``RINCON_B`` in the group
    a moment later (via a push-style event to the subscriber), and
    assert that ``create_session`` is issued *after* B appears."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    b_client.groups = [group]
    backend._clients["RINCON_B"] = b_client

    # Track the subscription callback so the test can fire a simulated
    # "group updated" event AFTER modify returns — mimicking Sonos's
    # push-event arrival that actually lands the new member.
    captured_callbacks: list = []

    def fake_subscribe(callback, event_filter=None, object_id_filter=None):
        captured_callbacks.append(callback)
        return lambda: None

    client.subscribe = fake_subscribe

    async def simulated_modify(
        _group_id: str,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> dict:
        # Return the GroupInfo but do NOT yet update group.player_ids —
        # that happens asynchronously when the push event fires.
        new_members = [
            *(p for p in group.player_ids if p not in player_ids_to_remove),
            *player_ids_to_add,
        ]

        # Schedule the cache update on the event loop so it lands
        # after modify_group_members returns — same shape as real
        # aiosonos push latency.
        async def _delayed_push() -> None:
            await asyncio.sleep(0.05)
            group.player_ids = new_members
            for cb in captured_callbacks:
                cb(None)

        asyncio.create_task(_delayed_push())
        return {
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": "group-1",
                "name": "Zone",
                "coordinatorId": "RINCON_A",
                "playerIds": new_members,
            },
        }

    client.api.groups.modify_group_members = AsyncMock(
        side_effect=simulated_modify
    )

    # Observation hook: record whether B was in the group at the
    # moment create_session was called.
    observed_members: list[list[str]] = []

    async def observing_create_session(_group_id: str, **_kwargs: object) -> dict:
        observed_members.append(list(group.player_ids))
        return {"sessionId": "session-1"}

    client.api.playback_session.create_session = AsyncMock(
        side_effect=observing_create_session
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    # Membership had converged before create_session fired.
    assert observed_members == [["RINCON_A", "RINCON_B"]], (
        f"create_session observed partial membership: {observed_members}"
    )


async def test_ensure_group_settles_via_polling_when_events_are_dropped() -> None:
    """On large households aiosonos occasionally swallows push events
    during the storm of updates that accompanies a full-house regroup.
    ``_wait_for_group`` polls ``client.groups`` as a fallback so a
    missed event doesn't wedge the wait — simulate that case by having
    ``modify_group_members`` mutate the group's ``player_ids`` without
    firing any subscription callbacks, and assert the wait still
    completes (instead of raising ``RuntimeError`` on timeout)."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    b_client.groups = [group]
    backend._clients["RINCON_B"] = b_client

    # Subscribe captures callbacks but we deliberately NEVER invoke
    # them — this is the "events got dropped" scenario.
    client.subscribe = lambda cb, **_kw: (lambda: None)

    async def simulated_modify(
        _group_id: str,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> dict:
        new_members = [
            *(p for p in group.player_ids if p not in player_ids_to_remove),
            *player_ids_to_add,
        ]

        # Update the cached group state after a brief delay, WITHOUT
        # firing any subscription callbacks. The poll loop inside
        # _wait_for_group must see this change.
        async def _delayed_update() -> None:
            await asyncio.sleep(0.05)
            group.player_ids = new_members

        asyncio.create_task(_delayed_update())
        return {
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": "group-1",
                "name": "Zone",
                "coordinatorId": "RINCON_A",
                "playerIds": new_members,
            },
        }

    client.api.groups.modify_group_members = AsyncMock(
        side_effect=simulated_modify
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    # If polling didn't kick in we'd have hung until the 30s topology
    # timeout and raised ``RuntimeError``; reaching here is the pass.
    client.api.playback_session.create_session.assert_awaited()


async def test_create_session_uses_elected_coordinator_not_anchor() -> None:
    """Sonos's local WebSocket API requires ``createSession`` to be
    issued through the **coordinator's** WebSocket — not through any
    group member. When ``modifyGroupMembers`` re-elects a different
    coordinator than our anchor (first target), the dispatch must
    route the session-create call through the elected coordinator's
    client. This is the root fix for the persistent
    ``groupCoordinatorChanged`` seen on real hardware."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, anchor_client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    # Build a second client for RINCON_B — this is the speaker Sonos
    # will elect as coordinator in the modify response.
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player_id = "RINCON_B"
    b_client.player = MagicMock(group=group, set_volume=AsyncMock())
    b_client.groups = [group]
    b_client.api = MagicMock()
    b_client.api.playback_session = MagicMock()
    b_client.api.playback_session.create_session = AsyncMock(
        return_value={"sessionId": "session-from-B"}
    )
    b_client.api.playback_session.load_stream_url = AsyncMock()
    backend._clients["RINCON_B"] = b_client

    # Arrange: modify_group_members returns a GroupInfo where Sonos
    # elected RINCON_B as coordinator, not our RINCON_A anchor. Also
    # mutate the group's player_ids in-place so _wait_for_group's
    # post-modify verification sees the fresh membership.
    async def simulated_modify(
        _group_id: str,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> dict:
        group.player_ids = [
            *(p for p in group.player_ids if p not in player_ids_to_remove),
            *player_ids_to_add,
        ]
        group.id = "group-new"
        group.coordinator_id = "RINCON_B"
        return {
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": "group-new",
                "name": "Group",
                "coordinatorId": "RINCON_B",
                "playerIds": list(group.player_ids),
            },
        }

    anchor_client.api.groups.modify_group_members = AsyncMock(
        side_effect=simulated_modify
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            speaker_ids=["RINCON_A", "RINCON_B"],
        )
    )

    # The session-create call went through B's WebSocket, not A's —
    # because Sonos elected B as the coordinator.
    b_client.api.playback_session.create_session.assert_awaited_once()
    assert (
        b_client.api.playback_session.create_session.call_args.args[0]
        == "group-new"
    )
    anchor_client.api.playback_session.create_session.assert_not_awaited()


async def test_duplicate_target_ids_deduped_before_sonos() -> None:
    """Callers sometimes produce lists with the same player_id twice
    (e.g. a speaker addressed by both its real name and an alias).
    Sonos rejects the topology command with "Effective set of new
    group members has repeated player id" — the backend must dedupe
    before forwarding."""
    group = MagicMock()
    group.id = "group-1"
    group.player_ids = ["RINCON_A"]
    group.coordinator_id = "RINCON_A"
    group.playback_state = "PLAYBACK_STATE_IDLE"

    backend, client, _p, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A",
        group_in=group,
    )
    backend._player_metadata["RINCON_B"] = _PlayerMetadata(
        player_id="RINCON_B",
        household_id="HH-TEST",
        name="Lounge",
        ip_address="192.168.1.21",
        model="Sonos One",
    )
    b_client = MagicMock()
    b_client.player = MagicMock(group=group)
    b_client.groups = [group]
    backend._clients["RINCON_B"] = b_client

    async def simulated_modify(
        _group_id: str,
        player_ids_to_add: list[str],
        player_ids_to_remove: list[str],
    ) -> dict:
        group.player_ids = [
            *(p for p in group.player_ids if p not in player_ids_to_remove),
            *player_ids_to_add,
        ]
        return {
            "_objectType": "groupInfo",
            "group": {
                "_objectType": "group",
                "id": "group-1",
                "name": "Zone",
                "coordinatorId": "RINCON_A",
                "playerIds": list(group.player_ids),
            },
        }

    client.api.groups.modify_group_members = AsyncMock(
        side_effect=simulated_modify
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/song.mp3",
            # A (real name) + A (alias) + B — RINCON_A appears twice.
            speaker_ids=["RINCON_A", "RINCON_A", "RINCON_B"],
        )
    )

    # Each player_id appears at most once in the modify diff.
    kwargs = client.api.groups.modify_group_members.call_args.kwargs
    assert kwargs["player_ids_to_add"] == ["RINCON_B"]
    assert kwargs["player_ids_to_remove"] == []


async def test_duplicate_announce_target_ids_deduped() -> None:
    """Same dedupe applies on the announce path — otherwise we'd fire
    ``play_audio_clip`` twice on the same speaker, which with slight
    timing skew plays the clip twice or raises an overlap error."""
    backend, _c, player, _g = _make_backend_with_mock_speaker(
        player_id="RINCON_A"
    )

    await backend.play_uri(
        PlayRequest(
            uri="http://gilbert/ding.mp3",
            speaker_ids=["RINCON_A", "RINCON_A", "RINCON_A"],
            announce=True,
        )
    )

    # Only one clip fired despite three appearances of the same id.
    assert player.play_audio_clip.await_count == 1


# ── list_speakers materialization ────────────────────────────────────


async def test_list_speakers_reflects_live_state() -> None:
    """list_speakers should pull the current volume + group from the
    client, not a cached snapshot taken at discovery time — otherwise
    a volume change via the Sonos app wouldn't show up until
    reconnection."""
    backend, _c, player, group = _make_backend_with_mock_speaker()
    player.volume_level = 85
    group.name = "Living Zone"
    group.id = "live-group-id"
    group.playback_state = "PLAYBACK_STATE_PLAYING"

    infos = await backend.list_speakers()
    assert len(infos) == 1
    info = infos[0]
    assert info.volume == 85
    assert info.group_name == "Living Zone"
    assert info.group_id == "live-group-id"
    assert info.state.value == "playing"
