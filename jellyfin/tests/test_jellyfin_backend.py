"""Tests for the Jellyfin backend.

Mapping tests use hand-curated JSON fixtures. HTTP tests use
``httpx.MockTransport`` — the *external* dependency, never our own
classes (per ``CLAUDE.md`` test rule "Don't mock the thing you're
supposed to be testing").
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from gilbert_plugin_jellyfin.jellyfin_backend import (  # type: ignore[import-not-found]
    JellyfinBackend,
    _jellyfin_to_media_item,
    _seconds_to_ticks,
    _session_to_client,
    _session_to_media_session,
    _ticks_to_seconds,
    _utc_seconds_from_iso,
)

from gilbert.interfaces.media_library import (
    MediaItem,
    MediaKind,
    MediaLibraryUnavailableError,
    MediaPlaybackState,
    MediaPlayCommand,
)

# ── Fixture loader ─────────────────────────────────────────────────


_FIX_DIR = Path(__file__).resolve().parent / "fixtures" / "jellyfin"


def _load(name: str) -> dict:
    return json.loads((_FIX_DIR / name).read_text())


# ── Mapping tests ──────────────────────────────────────────────────


def test_kind_from_jellyfin_movie() -> None:
    item = _jellyfin_to_media_item(
        _load("movie.json"),
        server_url="http://jellyfin.local",
        server_id="srv-1",
    )
    assert item.kind == MediaKind.MOVIE
    assert item.id == "abc-123-movie"
    assert item.title == "Dune (2021)"
    assert item.year == 2021
    # 95400000000 ticks → 9540 seconds
    assert item.duration_seconds == 9540.0
    assert item.rating == 8.0
    assert item.content_rating == "PG-13"
    assert item.studio == "Legendary"
    assert "Sci-Fi" in item.genres
    assert "Denis Villeneuve" in item.directors
    assert "Timothée Chalamet" in item.actors
    assert item.poster_url.startswith("http://jellyfin.local/Items/")
    assert "tag-primary" in item.poster_url
    assert item.backdrop_url.startswith("http://jellyfin.local/Items/")
    assert item.is_watched is True
    assert item.view_count == 1


def test_jellyfin_to_media_item_show() -> None:
    item = _jellyfin_to_media_item(
        _load("show.json"),
        server_url="http://j.local",
        server_id="srv-1",
    )
    assert item.kind == MediaKind.SHOW
    assert item.title == "Severance"
    assert item.year == 2022


def test_jellyfin_to_media_item_season() -> None:
    item = _jellyfin_to_media_item(
        _load("season.json"),
        server_url="http://j.local",
        server_id="srv-1",
    )
    assert item.kind == MediaKind.SEASON
    assert item.grandparent_id == "show-id-1"
    assert item.grandparent_title == "Severance"


def test_jellyfin_to_media_item_episode() -> None:
    item = _jellyfin_to_media_item(
        _load("episode.json"),
        server_url="http://j.local",
        server_id="srv-1",
    )
    assert item.kind == MediaKind.EPISODE
    assert item.title == "Cold Harbor"
    assert item.season_number == 2
    assert item.episode_number == 3
    assert item.grandparent_id == "show-id-1"
    assert item.grandparent_title == "Severance"
    # 6_000_000_000 ticks → 600 seconds
    assert item.view_offset_seconds == 600.0
    assert item.is_watched is False


def test_jellyfin_to_media_item_track() -> None:
    item = _jellyfin_to_media_item(
        _load("track.json"),
        server_url="http://j.local",
        server_id="srv-1",
    )
    assert item.kind == MediaKind.MUSIC_TRACK
    assert item.duration_seconds == 225.0


def test_jellyfin_to_media_item_normalizes_non_utc_datecreated() -> None:
    item = _jellyfin_to_media_item(
        _load("movie_non_utc.json"),
        server_url="http://j.local",
        server_id="srv-1",
    )
    # 2024-06-01T10:00:00-07:00 == 2024-06-01T17:00:00Z → 1717261200 unix
    assert item.added_at == 1717261200.0


def test_ticks_seconds_round_trip() -> None:
    assert _seconds_to_ticks(5.0) == 50_000_000
    assert _ticks_to_seconds(50_000_000) == 5.0
    assert _ticks_to_seconds(None) == 0.0


def test_session_to_media_session_renders_now_playing() -> None:
    sessions = _load("sessions_list.json")
    s1 = _session_to_media_session(
        sessions[0], server_url="http://j.local", server_id="srv-1"
    )
    assert s1 is not None
    assert s1.state == MediaPlaybackState.PLAYING
    assert s1.client.name == "Living Room TV"
    assert s1.client.supports_seek is True
    assert s1.position_seconds == 5000.0  # 50_000_000_000 / 10_000_000
    assert s1.item.title == "Dune (2021)"

    # Session without NowPlayingItem returns None.
    s2 = _session_to_media_session(
        sessions[1], server_url="http://j.local", server_id="srv-1"
    )
    assert s2 is None


def test_session_to_client() -> None:
    sessions = _load("sessions_list.json")
    c1 = _session_to_client(sessions[0], server_id="srv-1")
    assert c1.name == "Living Room TV"
    assert c1.supports_seek is True
    c2 = _session_to_client(sessions[1], server_id="srv-1")
    assert c2.supports_seek is False  # SupportedCommands empty


# ── Backend behavior ──────────────────────────────────────────────


def _make_backend_with_transport(transport: httpx.MockTransport) -> JellyfinBackend:
    backend = JellyfinBackend()
    backend._server_url = "http://jellyfin.local"
    backend._access_token = "tok"
    backend._http = httpx.AsyncClient(
        base_url="http://jellyfin.local",
        transport=transport,
        headers={"X-Emby-Authorization": 'MediaBrowser Token="tok"'},
    )
    return backend


def test_capability_flags_propagate() -> None:
    backend = JellyfinBackend()
    assert backend.supports_now_playing is True
    assert backend.supports_resume is True
    assert backend.supports_continue_watching is True
    assert backend.supports_recently_added is True
    assert backend.supports_seek is True
    assert backend.supports_per_user is True
    assert backend.supports_next_episode is True


def test_runtime_dependencies_empty() -> None:
    assert JellyfinBackend.runtime_dependencies() == []


async def test_link_account_persists_token_and_clears_password() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        return httpx.Response(
            200,
            json={"AccessToken": "newtok-123", "User": {"Id": "u1"}},
        )

    transport = httpx.MockTransport(handler)
    backend = JellyfinBackend()
    backend._server_url = "http://jellyfin.local"

    # Patch _link_account to use our transport.
    import gilbert_plugin_jellyfin.jellyfin_backend as mod

    orig_async_client = mod.httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    mod.httpx.AsyncClient = _patched_async_client  # type: ignore[assignment]
    try:
        result = await backend.invoke_backend_action(
            "link_account", {"username": "admin", "password": "secret"}
        )
    finally:
        mod.httpx.AsyncClient = orig_async_client  # type: ignore[assignment]

    assert result.status == "ok"
    assert result.data["persist"]["access_token"] == "newtok-123"
    assert result.data["persist"]["admin_password"] == ""
    assert captured["path"] == "/Users/AuthenticateByName"


async def test_link_account_keeps_password_when_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"AccessToken": "tok"})

    backend = JellyfinBackend()
    backend._server_url = "http://j.local"
    transport = httpx.MockTransport(handler)
    import gilbert_plugin_jellyfin.jellyfin_backend as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **kw: orig(  # type: ignore[assignment]
        *a, **{**kw, "transport": transport}
    )
    try:
        result = await backend.invoke_backend_action(
            "link_account",
            {"username": "admin", "password": "x", "keep_password": True},
        )
    finally:
        mod.httpx.AsyncClient = orig  # type: ignore[assignment]
    assert result.status == "ok"
    assert "admin_password" not in result.data["persist"]


async def test_recently_added_translates_latest_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[_load("movie.json")])

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    out = await backend.recently_added(backend_user_id="u1", limit=3)
    assert len(out) == 1
    assert "Items/Latest" in captured["path"]
    assert captured["params"]["Limit"] == "3"


async def test_continue_watching_marks_next_up() -> None:
    ep = _load("episode.json")
    # Force offset 0 → next_up=True for an episode.
    ep["UserData"]["PlaybackPositionTicks"] = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [ep]})

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    out = await backend.continue_watching(backend_user_id="u1", limit=5)
    assert len(out) == 1
    assert out[0].next_up is True


async def test_continue_watching_no_user_raises() -> None:
    backend = _make_backend_with_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(MediaLibraryUnavailableError):
        await backend.continue_watching(backend_user_id="")


async def test_play_constructs_session_url_with_ticks() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(204)

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    item = MediaItem(
        id="abc-123",
        backend_name="jellyfin",
        server_id="srv-1",
        title="X",
        kind=MediaKind.MOVIE,
    )
    from gilbert.interfaces.media_library import MediaClient

    client = MediaClient(
        client_id="sess-1",
        backend_name="jellyfin",
        server_id="srv-1",
        name="TV",
    )
    cmd = MediaPlayCommand(item=item, client=client, offset_seconds=12.0)
    await backend.play(cmd)
    assert captured["path"] == "/Sessions/sess-1/Playing"
    assert captured["params"]["ItemIds"] == "abc-123"
    assert captured["params"]["PlayCommand"] == "PlayNow"
    assert captured["params"]["StartPositionTicks"] == "120000000"


async def test_seek_translates_seconds_to_ticks() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(204)

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    await backend.seek("sess-1", 5.0)
    assert captured["path"] == "/Sessions/sess-1/Playing/Seek"
    assert captured["params"]["SeekPositionTicks"] == "50000000"


async def test_pause_resume_stop_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(204)

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    await backend.pause("sess-1")
    await backend.resume("sess-1")
    await backend.stop("sess-1")
    assert seen == [
        "/Sessions/sess-1/Playing/Pause",
        "/Sessions/sess-1/Playing/Unpause",
        "/Sessions/sess-1/Playing/Stop",
    ]


async def test_next_episode_returns_nextup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/NextUp" in request.url.path:
            return httpx.Response(200, json={"Items": [_load("episode.json")]})
        return httpx.Response(404)

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    out = await backend.next_episode("show-id-1", backend_user_id="u1")
    assert out is not None
    assert out.id == "episode-id-23"
    assert out.episode_number == 3


async def test_next_episode_falls_back_to_episodes_when_nextup_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/NextUp" in request.url.path:
            return httpx.Response(200, json={"Items": []})
        if "/Episodes" in request.url.path:
            return httpx.Response(200, json={"Items": [_load("episode.json")]})
        return httpx.Response(404)

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    out = await backend.next_episode("show-id-1", backend_user_id="u1")
    assert out is not None
    assert out.id == "episode-id-23"


async def test_next_episode_returns_none_when_caught_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": []})

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    out = await backend.next_episode("show-id-1", backend_user_id="u1")
    assert out is None


async def test_unauthorized_translates_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "token expired"})

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    with pytest.raises(MediaLibraryUnavailableError) as exc:
        await backend.list_clients()
    assert "token revoked" in str(exc.value)


async def test_resolve_user_id_caches_by_jellyfin_username() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json=[
                {"Id": "u-alice", "Name": "alice"},
                {"Id": "u-bob", "Name": "bob"},
            ],
        )

    backend = _make_backend_with_transport(httpx.MockTransport(handler))
    uid_a = await backend._resolve_user_id("alice")
    uid_a2 = await backend._resolve_user_id("alice")
    uid_b = await backend._resolve_user_id("bob")
    assert uid_a == "u-alice"
    assert uid_a2 == "u-alice"
    assert uid_b == "u-bob"
    # First two calls hit /Users; the third is also fresh because we
    # didn't cache between alice and bob lookups (cache hits skip).
    # But alice's second call is cached.
    assert call_count == 2  # alice (miss), bob (miss); alice2 cached.


def test_utc_seconds_handles_z_and_offset_isoforms() -> None:
    assert _utc_seconds_from_iso("2024-01-15T10:00:00Z") == 1705312800.0
    assert _utc_seconds_from_iso("2024-01-15T10:00:00.000Z") == 1705312800.0
    # Same instant in -07:00:
    assert _utc_seconds_from_iso("2024-01-15T03:00:00-07:00") == 1705312800.0
    assert _utc_seconds_from_iso("") == 0.0
    assert _utc_seconds_from_iso(None) == 0.0
