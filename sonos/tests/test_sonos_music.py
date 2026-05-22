"""Tests for the Spotify Web API-backed music backend.

Post-migration architecture: search + browse hit Spotify's Web API
directly via OAuth; playback hands the resulting ``spotify:*:*`` URI
to the speaker backend, which renders via the speaker's linked
Spotify account. These tests cover the mapping surface (Spotify JSON
→ MusicItem), the auth-code extraction from whatever the user
pastes, and that resolve_playable hands back a playable Spotify URI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_sonos.sonos_music import (
    SonosMusic,
    _extract_auth_code,
    _spotify_album_to_music_item,
    _spotify_artist_to_music_item,
    _spotify_playlist_to_music_item,
    _spotify_track_to_music_item,
)

from gilbert.interfaces.music import (
    MusicItem,
    MusicItemKind,
    MusicSearchUnavailableError,
)

# ── _extract_auth_code ──────────────────────────────────────────────


def test_extract_auth_code_from_full_redirect_url() -> None:
    """Most common flow: user pastes the entire URL their browser
    landed on after approving Spotify."""
    url = "http://localhost:8888/callback?code=AQCxxxxxxx&state=abc"
    assert _extract_auth_code(url) == "AQCxxxxxxx"


def test_extract_auth_code_from_query_fragment() -> None:
    """Users sometimes paste just the ``?code=...`` slice."""
    assert _extract_auth_code("?code=AQDyyyy&state=xyz") == "AQDyyyy"


def test_extract_auth_code_from_bare_code() -> None:
    """And sometimes only the code token itself."""
    code = "A" * 200
    assert _extract_auth_code(code) == code


def test_extract_auth_code_empty_and_junk() -> None:
    assert _extract_auth_code("") == ""
    assert _extract_auth_code("   ") == ""
    assert _extract_auth_code("hello world") == ""


# ── Spotify JSON → MusicItem ────────────────────────────────────────


def test_track_mapping_populates_core_fields() -> None:
    track = {
        "id": "3w0pyHgJJW9JN0cJxmi33Z",
        "name": "Always and Forever",
        "uri": "spotify:track:3w0pyHgJJW9JN0cJxmi33Z",
        "duration_ms": 290_000,
        "artists": [{"name": "Heatwave"}],
        "album": {
            "images": [{"url": "https://i.scdn.co/image/abc"}],
        },
    }
    item = _spotify_track_to_music_item(track)
    assert item.id == "3w0pyHgJJW9JN0cJxmi33Z"
    assert item.title == "Always and Forever"
    assert item.kind == MusicItemKind.TRACK
    assert item.subtitle == "Heatwave"
    assert item.uri == "spotify:track:3w0pyHgJJW9JN0cJxmi33Z"
    assert item.album_art_url == "https://i.scdn.co/image/abc"
    assert abs(item.duration_seconds - 290.0) < 0.01
    assert item.service == "Spotify"


def test_track_mapping_tolerates_missing_fields() -> None:
    """Spotify occasionally returns tracks with no album (compilation
    tracks, unavailable content) — mapper shouldn't explode."""
    track = {
        "id": "x",
        "name": "Mystery",
        "uri": "spotify:track:x",
    }
    item = _spotify_track_to_music_item(track)
    assert item.title == "Mystery"
    assert item.subtitle == ""
    assert item.album_art_url == ""
    assert item.duration_seconds == 0.0


def test_album_mapping() -> None:
    item = _spotify_album_to_music_item(
        {
            "id": "abc",
            "name": "Rumours",
            "uri": "spotify:album:abc",
            "artists": [{"name": "Fleetwood Mac"}],
        }
    )
    assert item.kind == MusicItemKind.ALBUM
    assert item.title == "Rumours"
    assert item.subtitle == "Fleetwood Mac"


def test_artist_mapping() -> None:
    item = _spotify_artist_to_music_item(
        {
            "id": "1dfeR4HaWDbWqFHLkxsg1d",
            "name": "Queen",
            "uri": "spotify:artist:1dfeR4HaWDbWqFHLkxsg1d",
        }
    )
    assert item.kind == MusicItemKind.ARTIST
    assert item.title == "Queen"


def test_playlist_mapping_uses_owner_as_subtitle() -> None:
    item = _spotify_playlist_to_music_item(
        {
            "id": "37i9dQZF1DX",
            "name": "Release Radar",
            "uri": "spotify:playlist:37i9dQZF1DX",
            "owner": {"display_name": "Spotify"},
        }
    )
    assert item.kind == MusicItemKind.PLAYLIST
    assert item.subtitle == "Spotify"


# ── resolve_playable ────────────────────────────────────────────────


async def test_resolve_playable_passes_through_uri() -> None:
    """When the MusicItem already has a Spotify URI, resolve just
    hands it back as a Playable — no extra API call needed."""
    backend = SonosMusic()
    item = MusicItem(
        id="abc",
        title="Test",
        kind=MusicItemKind.TRACK,
        uri="spotify:track:abc",
    )
    playable = await backend.resolve_playable(item)
    assert playable.uri == "spotify:track:abc"
    assert playable.didl_meta == ""
    assert playable.title == "Test"


async def test_resolve_playable_reconstructs_from_id_when_no_uri() -> None:
    """Items synthesised by other callers (no uri, id only) should
    still become playable — build the URI from kind + id."""
    backend = SonosMusic()
    item = MusicItem(
        id="abc",
        title="Test",
        kind=MusicItemKind.PLAYLIST,
        uri="",
    )
    playable = await backend.resolve_playable(item)
    assert playable.uri == "spotify:playlist:abc"


async def test_resolve_playable_raises_when_nothing_resolvable() -> None:
    backend = SonosMusic()
    item = MusicItem(
        id="",
        title="Ghost",
        kind=MusicItemKind.TRACK,
        uri="",
    )
    with pytest.raises(ValueError, match="no uri and no id"):
        await backend.resolve_playable(item)


# ── list_linked_services ────────────────────────────────────────────


def test_list_linked_services_reports_spotify_when_linked() -> None:
    backend = SonosMusic()
    backend._refresh_token = "valid-refresh-token"
    assert backend.list_linked_services() == ["Spotify"]


def test_list_linked_services_empty_when_not_linked() -> None:
    backend = SonosMusic()
    backend._refresh_token = ""
    assert backend.list_linked_services() == []


# ── Search routing ──────────────────────────────────────────────────


async def test_search_without_credentials_raises_unavailable() -> None:
    """Before Spotify is configured, search should surface a clear
    message telling the admin to run the link flow — not a generic
    HTTP/attribute error."""
    backend = SonosMusic()
    await backend.initialize({})
    with pytest.raises(MusicSearchUnavailableError, match="Spotify"):
        await backend.search("anything", kind=MusicItemKind.TRACK)


async def test_search_maps_track_results() -> None:
    """Verify TRACK kind wires through to Spotify's ``search?type=track``
    and the result mapper in one call."""
    backend = SonosMusic()
    await backend.initialize(
        {"client_id": "cid", "client_secret": "csec"}
    )
    fake_spotify = MagicMock()
    fake_spotify.search = AsyncMock(
        return_value=[
            {
                "id": "1",
                "name": "Song A",
                "uri": "spotify:track:1",
                "duration_ms": 120_000,
                "artists": [{"name": "Artist A"}],
                "album": {"images": []},
            },
        ]
    )
    backend._spotify = fake_spotify

    results = await backend.search(
        "hello world", kind=MusicItemKind.TRACK, limit=5
    )

    fake_spotify.search.assert_awaited_once_with("hello world", "track", 5)
    assert len(results) == 1
    assert results[0].title == "Song A"
    assert results[0].kind == MusicItemKind.TRACK
    assert results[0].uri == "spotify:track:1"


async def test_search_station_maps_to_playlist() -> None:
    """STATION isn't a Spotify concept — we map it to playlist so
    ``/music search stations`` surfaces editorial playlists, which
    are the closest analogue Spotify offers."""
    backend = SonosMusic()
    await backend.initialize(
        {"client_id": "cid", "client_secret": "csec"}
    )
    fake_spotify = MagicMock()
    fake_spotify.search = AsyncMock(return_value=[])
    backend._spotify = fake_spotify

    await backend.search("jazz", kind=MusicItemKind.STATION, limit=3)
    fake_spotify.search.assert_awaited_once_with("jazz", "playlist", 3)


# ── Stations ──────────────────────────────────────────────────────────


async def test_start_station_with_track_item_uses_seed_tracks() -> None:
    """A ``MusicItem`` of kind TRACK feeds straight into Spotify's
    ``seed_tracks`` parameter — no second search needed when the
    caller already has a resolved track in hand."""
    backend = SonosMusic()
    await backend.initialize({"client_id": "cid", "client_secret": "csec"})
    fake_spotify = MagicMock()
    fake_spotify.recommendations = AsyncMock(
        return_value=[
            {
                "id": "rec1",
                "name": "Recommended",
                "uri": "spotify:track:rec1",
                "duration_ms": 200_000,
                "artists": [{"name": "Some Artist"}],
                "album": {"images": []},
            }
        ]
    )
    backend._spotify = fake_spotify

    seed = MusicItem(
        id="seed-id", title="Seed Song", kind=MusicItemKind.TRACK,
        uri="spotify:track:seed-id",
    )
    results = await backend.start_station(seed, limit=5)

    fake_spotify.recommendations.assert_awaited_once_with(
        seed_tracks=["seed-id"],
        seed_artists=None,
        seed_genres=None,
        limit=5,
    )
    assert len(results) == 1
    assert results[0].kind == MusicItemKind.TRACK


async def test_start_station_with_freetext_falls_back_to_artist_search() -> None:
    """For a free-text seed we try genre seeds first; on no match the
    backend searches Spotify for an artist by that name and uses its
    id as ``seed_artists`` — covers 'play a station based on Wilco'
    where the seed isn't a Spotify genre keyword."""
    backend = SonosMusic()
    await backend.initialize({"client_id": "cid", "client_secret": "csec"})
    fake_spotify = MagicMock()
    fake_spotify.available_genre_seeds = AsyncMock(return_value=["jazz", "rock"])
    fake_spotify.search = AsyncMock(
        return_value=[{"id": "artist-42", "name": "Wilco"}]
    )
    fake_spotify.recommendations = AsyncMock(return_value=[])
    backend._spotify = fake_spotify

    await backend.start_station("Wilco", limit=10)

    fake_spotify.search.assert_awaited_with("Wilco", "artist", 1)
    fake_spotify.recommendations.assert_awaited_once_with(
        seed_tracks=None,
        seed_artists=["artist-42"],
        seed_genres=None,
        limit=10,
    )


async def test_start_station_404_surfaces_legacy_app_error() -> None:
    """Spotify deprecated /recommendations for new apps in late 2024 —
    a 404 from this endpoint means the user's Spotify app doesn't
    have legacy access. Surface that as an actionable message rather
    than a raw HTTPStatusError."""
    import httpx

    backend = SonosMusic()
    await backend.initialize({"client_id": "cid", "client_secret": "csec"})
    fake_spotify = MagicMock()
    fake_spotify.available_genre_seeds = AsyncMock(return_value=["jazz"])
    fake_spotify.recommendations = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
    )
    backend._spotify = fake_spotify

    seed = MusicItem(id="t", title="t", kind=MusicItemKind.TRACK, uri="spotify:track:t")
    with pytest.raises(MusicSearchUnavailableError, match="legacy access"):
        await backend.start_station(seed)


# ── Config params / actions surface ─────────────────────────────────


def test_config_params_include_spotify_oauth_fields() -> None:
    """Users need to see client_id, client_secret, refresh_token, and
    the spotify_auth_code paste field in the settings UI."""
    keys = {p.key for p in SonosMusic.backend_config_params()}
    # New fields for Spotify Web API pipeline.
    assert "client_id" in keys
    assert "client_secret" in keys
    assert "redirect_uri" in keys
    assert "refresh_token" in keys
    assert "spotify_auth_code" in keys
    # Legacy fields retained so existing configs don't fail validation.
    assert "preferred_service" in keys
    assert "auth_token" in keys
    assert "auth_key" in keys


def test_backend_actions_include_link_flow() -> None:
    action_keys = {a.key for a in SonosMusic.backend_actions()}
    assert action_keys == {
        "link_spotify",
        "link_spotify_complete",
        "test_connection",
    }


async def test_link_start_requires_client_id_and_secret() -> None:
    """Can't start OAuth without app credentials — error early with
    a message pointing the user at the Spotify developer dashboard."""
    backend = SonosMusic()
    await backend.initialize({})
    result = await backend._action_link_start()
    assert result.status == "error"
    assert "Client ID" in result.message


async def test_link_complete_without_code_errors() -> None:
    """If the user clicks Finish Linking without pasting a code in
    first, we error clearly rather than making a bogus token
    request."""
    backend = SonosMusic()
    await backend.initialize(
        {"client_id": "cid", "client_secret": "csec"}
    )
    # Simulate the in-progress state — the link_spotify action would
    # have initialized self._spotify.
    # Empty payload == no auth code pasted.
    result = await backend._action_link_complete({})
    assert result.status == "error"
    assert "authorization code" in result.message.lower()


async def test_link_complete_reads_auth_code_from_saved_settings() -> None:
    """The UI's ConfigAction invocation payload is empty — config
    values live in the settings dict passed to ``initialize``, which
    MusicService re-runs after each save. The backend has to cache the
    ``spotify_auth_code`` at init time and pick it up from there on
    the next link_complete click; pulling from the empty payload alone
    would always error."""
    from unittest.mock import AsyncMock

    backend = SonosMusic()
    await backend.initialize(
        {
            "client_id": "cid",
            "client_secret": "csec",
            # The redirect URL the user pasted after approving; the
            # manual-paste flow runs it through _extract_auth_code.
            "spotify_auth_code": (
                "http://127.0.0.1:8000/callback?code=AQXYZ&state=abc"
            ),
        }
    )

    # Stub out the token exchange so we don't actually hit Spotify;
    # we just want to confirm the extracted code gets handed in.
    backend._spotify.exchange_code = AsyncMock()  # type: ignore[method-assign]
    backend._spotify._refresh_token = "refreshed"  # type: ignore[attr-defined]

    # Empty action payload — the normal case from the UI.
    result = await backend._action_link_complete({})

    assert result.status == "ok"
    backend._spotify.exchange_code.assert_awaited_once()
    # Code was extracted from the URL stored in settings.
    called_code = backend._spotify.exchange_code.call_args.args[0]
    assert called_code == "AQXYZ"
    # Result should persist the refresh token + clear the paste field.
    persist = result.data["persist"]
    assert persist["settings.refresh_token"] == "refreshed"
    assert persist["settings.spotify_auth_code"] == ""


# ── compatible_speaker_backends ────────────────────────────────────────


def test_sonos_music_only_compatible_with_sonos_speakers() -> None:
    """SonosMusic produces Spotify URIs that only Sonos speakers can
    consume, so it declares sonos-specific compatibility."""
    assert SonosMusic.compatible_speaker_backends() == frozenset({"sonos"})
