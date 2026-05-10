"""Tests for the Plex backend.

Mapping tests use hand-curated XML fixtures parsed into mock plexapi-
shaped attribute objects. Method tests stub ``plexapi.PlexServer`` and
``plexapi.MyPlexAccount`` via ``unittest.mock`` — the *external*
dependency, never our own classes (per ``CLAUDE.md`` test rule
"Don't mock the thing you're supposed to be testing").
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest
from gilbert_plugin_plex.plex_backend import (  # type: ignore[import-not-found]
    PlexBackend,
    _kind_from_plex,
    _plex_to_media_item,
    _utc_seconds_from_addedat,
)

from gilbert.interfaces.media_library import (
    MediaItem,
    MediaKind,
    MediaLibraryUnavailableError,
    MediaPlayCommand,
)

# ── Fixture loader ────────────────────────────────────────────────


_FIX_DIR = Path(__file__).resolve().parent / "fixtures" / "plex"


class _AttrObj:
    """Mock object with attribute access mirroring plexapi's surface.

    Built from XML element attributes plus child <Genre>/<Role>/etc.
    tags. Supports ``getattr`` for missing attributes returning ``None``.
    """

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _load_fixture(name: str) -> _AttrObj:
    """Parse a fixture XML and build a plexapi-shaped attribute object."""
    tree = ET.parse(_FIX_DIR / name)
    root = tree.getroot()
    # First child <Video|Directory|Track>
    elem = next(iter(root))
    attrs = dict(elem.attrib)

    obj = _AttrObj()

    # Numeric coercions for fields plexapi auto-coerces.
    for int_field in (
        "ratingKey",
        "year",
        "duration",
        "addedAt",
        "lastViewedAt",
        "viewCount",
        "viewOffset",
        "parentRatingKey",
        "grandparentRatingKey",
        "index",
        "parentIndex",
    ):
        if int_field in attrs:
            raw = attrs[int_field]
            try:
                setattr(obj, int_field, int(raw))
            except ValueError:
                # Some fixtures encode timestamps as ISO-8601 to
                # exercise the ``_utc_seconds_from_addedat`` datetime
                # branch. Parse to a tz-aware ``datetime`` so the
                # non-UTC normalization path actually runs.
                if int_field in ("addedAt", "lastViewedAt"):
                    try:
                        # ``datetime.fromisoformat`` handles "+HH:MM"
                        # offsets natively in 3.11+.
                        setattr(obj, int_field, _dt.datetime.fromisoformat(raw))
                    except ValueError:
                        setattr(obj, int_field, raw)
                else:
                    setattr(obj, int_field, raw)
            del attrs[int_field]
    for float_field in ("rating",):
        if float_field in attrs:
            try:
                setattr(obj, float_field, float(attrs[float_field]))
            except ValueError:
                setattr(obj, float_field, attrs[float_field])
            del attrs[float_field]

    for k, v in attrs.items():
        setattr(obj, k, v)

    # Genres / Directors / Roles are list-of-tag-objects.
    obj.genres = []
    obj.directors = []
    obj.roles = []
    for child in elem:
        if child.tag == "Genre":
            obj.genres.append(_AttrObj(tag=child.attrib.get("tag", "")))
        elif child.tag == "Director":
            obj.directors.append(_AttrObj(tag=child.attrib.get("tag", "")))
        elif child.tag == "Role":
            obj.roles.append(_AttrObj(tag=child.attrib.get("tag", "")))

    # plexapi exposes seasonNumber on Episodes — derived from
    # parentIndex.
    if (
        getattr(obj, "type", "") == "episode"
        and getattr(obj, "parentIndex", None) is not None
    ):
        obj.seasonNumber = obj.parentIndex

    # Build computed thumbUrl / artUrl as plexapi does — we just supply
    # placeholders here for the mapping helper.
    if hasattr(obj, "thumb"):
        obj.thumbUrl = f"http://plex.local{obj.thumb}?X-Plex-Token=fake"
    if hasattr(obj, "art"):
        obj.artUrl = f"http://plex.local{obj.art}?X-Plex-Token=fake"

    return obj


# ── Mapping tests ──────────────────────────────────────────────────


def test_kind_from_plex_movie() -> None:
    obj = _load_fixture("movie.xml")
    assert _kind_from_plex(obj) == MediaKind.MOVIE


def test_kind_from_plex_unknown_returns_unknown() -> None:
    assert _kind_from_plex(_AttrObj()) == MediaKind.UNKNOWN
    assert _kind_from_plex(_AttrObj(type="weird")) == MediaKind.UNKNOWN


def test_plex_to_media_item_movie() -> None:
    obj = _load_fixture("movie.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.id == "12345"
    assert item.backend_name == "plex"
    assert item.server_id == "srv-1"
    assert item.title == "Dune: Part Two"
    assert item.kind == MediaKind.MOVIE
    assert item.year == 2024
    assert item.duration_seconds == 9960.0
    assert item.rating == 8.5
    assert item.content_rating == "PG-13"
    assert "Sci-Fi" in item.genres
    assert "Adventure" in item.genres
    assert "Denis Villeneuve" in item.directors
    assert "Timothée Chalamet" in item.actors
    assert item.poster_url.endswith("X-Plex-Token=fake")
    assert item.added_at == 1717545600.0
    assert item.last_viewed_at == 1718000000.0
    assert item.view_count == 1
    assert item.is_watched is True


def test_plex_to_media_item_show() -> None:
    obj = _load_fixture("show.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.SHOW
    assert item.title == "Severance"
    assert item.year == 2022
    assert item.added_at == 1700000000.0


def test_plex_to_media_item_season() -> None:
    obj = _load_fixture("season.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.SEASON
    assert item.parent_id == "22345"
    assert item.parent_title == "Severance"


def test_plex_to_media_item_episode() -> None:
    obj = _load_fixture("episode.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.EPISODE
    assert item.title == "Cold Harbor"
    assert item.season_number == 2
    assert item.episode_number == 3
    assert item.parent_id == "22346"
    assert item.parent_title == "Season 2"
    assert item.grandparent_id == "22345"
    assert item.grandparent_title == "Severance"
    assert item.view_offset_seconds == 600.0
    assert item.is_watched is False


def test_plex_to_media_item_artist() -> None:
    obj = _load_fixture("artist.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.MUSIC_ARTIST
    assert item.title == "Adele"


def test_plex_to_media_item_album() -> None:
    obj = _load_fixture("album.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.MUSIC_ALBUM
    assert item.parent_title == "Adele"


def test_plex_to_media_item_track() -> None:
    obj = _load_fixture("track.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    assert item.kind == MediaKind.MUSIC_TRACK
    assert item.duration_seconds == 225.0


def test_plex_to_media_item_normalizes_non_utc_addedat() -> None:
    """Server timezone normalization — addedAt is UTC unix seconds at
    the mapping boundary regardless of how the server reports it.

    The fixture's ``addedAt="2024-06-01T10:00:00-07:00"`` is parsed
    as a tz-aware datetime by the loader, so the mapping helper hits
    the ``isinstance(value, datetime)`` branch and normalizes the
    -07:00 offset to UTC.
    """
    obj = _load_fixture("movie_non_utc.xml")
    item = _plex_to_media_item(obj, server_id="srv-1")
    # 2024-06-01T10:00:00-07:00 → 2024-06-01T17:00:00+00:00 → 1717261200.
    expected_utc = _dt.datetime(
        2024, 6, 1, 17, 0, 0, tzinfo=_dt.UTC
    ).timestamp()
    assert item.added_at == expected_utc

    # Direct unit test of the helper — naive datetime is treated as
    # UTC (mirrors the integer ``addedAt`` path).
    naive = _dt.datetime(2024, 6, 1, 17, 0, 0)
    assert (
        _utc_seconds_from_addedat(naive)
        == naive.replace(tzinfo=_dt.UTC).timestamp()
    )

    # And a tz-aware datetime in a different offset normalizes.
    aware_pdt = _dt.datetime(
        2024, 6, 1, 10, 0, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=-7))
    )
    assert (
        _utc_seconds_from_addedat(aware_pdt)
        == aware_pdt.astimezone(_dt.UTC).timestamp()
    )


# ── Capability flags ──────────────────────────────────────────────


def test_capability_flags_propagate() -> None:
    backend = PlexBackend()
    assert backend.supports_now_playing is True
    assert backend.supports_resume is True
    assert backend.supports_continue_watching is True
    assert backend.supports_recently_added is True
    assert backend.supports_seek is True
    assert backend.supports_per_user is True
    assert backend.supports_next_episode is True


def test_runtime_dependencies_empty() -> None:
    assert PlexBackend.runtime_dependencies() == []


# ── Per-user lock concurrency ─────────────────────────────────────


async def test_per_home_user_lock_does_not_serialize_across_users() -> None:
    """Two concurrent calls for *different* Home users must run in
    parallel (each gets its own per-user lock; the outer dict-guard
    lock is released as soon as the per-user lock is acquired).

    The same-user-serializes half lives in
    ``test_per_home_user_lock_serializes_same_user`` below.
    """
    backend = PlexBackend()
    backend._account_token = "fake"
    backend._machine_id = "srv-mach-id"

    # Build a fake account / server.
    fake_server = MagicMock()
    fake_server.machineIdentifier = "srv-mach-id"
    fake_server.url = "http://fake.local"
    backend._server = fake_server

    fake_account = MagicMock()
    backend._account = fake_account

    call_log: list[tuple[str, str]] = []
    enter_events: dict[str, asyncio.Event] = {}
    proceed_events: dict[str, asyncio.Event] = {}

    def _user_for(uid: str):
        # plexapi's account.user(uid) returns an object with get_token.
        user_obj = MagicMock()

        def _get_token(machine_id: str) -> str:
            ev_in = enter_events[uid]
            ev_in.set()
            # block until proceed fires
            ev_proceed = proceed_events[uid]
            # Sleep until proceed is set — non-async wait OK because
            # plexapi's call is wrapped in to_thread so this runs in
            # a worker thread.
            ev_proceed.wait()
            call_log.append(("get_token", uid))
            return f"token_{uid}"

        user_obj.get_token = _get_token
        return user_obj

    fake_account.user = _user_for

    # PlexServer constructor returns a per-user server.

    monkey_server_calls: list[str] = []

    def _fake_plex_server_ctor(url: str, token: str = "") -> Any:
        monkey_server_calls.append(token)
        s = MagicMock()
        s.url = url
        s.machineIdentifier = "srv-mach-id"
        return s

    # Patch plexapi.server.PlexServer at the module level via
    # monkeypatch-friendly attribute write.
    import plexapi.server as ps_mod

    original_ctor = ps_mod.PlexServer
    ps_mod.PlexServer = _fake_plex_server_ctor  # type: ignore[assignment]
    try:
        for uid in ("uA", "uB"):
            enter_events[uid] = asyncio.Event()
            proceed_events[uid] = asyncio.Event()

        async def _call(uid: str):
            return await backend._get_user_server(uid)

        # Spawn two concurrent calls for DIFFERENT users — each gets a
        # different lock; both should enter get_token in parallel.
        from threading import Event as _ThreadEvent
        # Need real thread Events because to_thread runs in a thread.
        for uid in ("uA", "uB"):
            ev_in = _ThreadEvent()
            ev_proceed = _ThreadEvent()
            enter_events[uid] = ev_in  # type: ignore[assignment]
            proceed_events[uid] = ev_proceed  # type: ignore[assignment]

        task_a = asyncio.create_task(_call("uA"))
        task_b = asyncio.create_task(_call("uB"))

        # Wait for both fetches to *enter* concurrently (not strict
        # parallel-time — just that both enter before we let either
        # proceed).
        loop = asyncio.get_running_loop()
        for uid in ("uA", "uB"):
            await loop.run_in_executor(
                None, enter_events[uid].wait
            )
        # Both have entered — release them.
        for uid in ("uA", "uB"):
            proceed_events[uid].set()
        await asyncio.gather(task_a, task_b)

        assert {c[1] for c in call_log} == {"uA", "uB"}
        assert backend._user_tokens["uA"] == "token_uA"
        assert backend._user_tokens["uB"] == "token_uB"
    finally:
        ps_mod.PlexServer = original_ctor  # type: ignore[assignment]


async def test_per_home_user_lock_serializes_same_user() -> None:
    """Two concurrent ``_get_user_server("uA")`` calls must serialize:
    the second waits for the first to populate the cache, then returns
    the cached server. The underlying ``account.user("uA").get_token``
    must be called *exactly once* — not twice.
    """
    backend = PlexBackend()
    backend._account_token = "fake"
    backend._machine_id = "srv-mach-id"

    fake_server = MagicMock()
    fake_server.machineIdentifier = "srv-mach-id"
    fake_server.url = "http://fake.local"
    backend._server = fake_server

    fake_account = MagicMock()
    backend._account = fake_account

    from threading import Event as _ThreadEvent

    # account.user("uA") returns the same mock both times so we can
    # observe call counts on the SAME object.
    user_obj = MagicMock()
    proceed = _ThreadEvent()
    call_log: list[str] = []

    def _get_token(machine_id: str) -> str:
        call_log.append("get_token")
        # Block the first call until the test releases it; the second
        # call (if the lock serializes correctly) never reaches here
        # because the cache hit returns first.
        proceed.wait()
        return "token_uA"

    user_obj.get_token = _get_token
    fake_account.user = MagicMock(return_value=user_obj)

    server_ctor_calls: list[str] = []

    def _fake_plex_server_ctor(url: str, token: str = "") -> Any:
        server_ctor_calls.append(token)
        s = MagicMock()
        s.url = url
        s.machineIdentifier = "srv-mach-id"
        return s

    import plexapi.server as ps_mod

    original_ctor = ps_mod.PlexServer
    ps_mod.PlexServer = _fake_plex_server_ctor  # type: ignore[assignment]
    try:
        async def _call() -> Any:
            return await backend._get_user_server("uA")

        task1 = asyncio.create_task(_call())
        task2 = asyncio.create_task(_call())
        # Yield a few times so both tasks reach the lock; the first
        # acquires it and blocks inside the to_thread get_token call.
        for _ in range(5):
            await asyncio.sleep(0)
        # Only ONE call into get_token should be in flight — the second
        # task is waiting on the per-user lock.
        assert call_log == ["get_token"]
        # Release the first call.
        proceed.set()
        result1 = await task1
        result2 = await task2
        # Cache hit: the second call returned the cached server; no
        # extra get_token call.
        assert call_log == ["get_token"]
        assert fake_account.user.call_count == 1
        # Both calls returned the SAME server instance (cached).
        assert result1 is result2
        # PlexServer constructor only called once for that token.
        assert server_ctor_calls == ["token_uA"]
    finally:
        ps_mod.PlexServer = original_ctor  # type: ignore[assignment]


async def test_account_token_rotation_clears_per_user_caches() -> None:
    backend = PlexBackend()
    backend._account_token = "old"
    backend._user_tokens = {"uA": "tA", "uB": "tB"}
    backend._user_servers = {"uA": MagicMock(), "uB": MagicMock()}
    backend._user_locks = {"uA": asyncio.Lock(), "uB": asyncio.Lock()}

    # initialize() with a different token should clear all per-Home-user
    # state. Stub plexapi's MyPlexAccount + PlexServer to cheap fakes.
    import plexapi.myplex as mp_mod
    import plexapi.server as ps_mod

    mp_orig = mp_mod.MyPlexAccount
    ps_orig = ps_mod.PlexServer
    mp_mod.MyPlexAccount = MagicMock(return_value=MagicMock())  # type: ignore[assignment]
    ps_mod.PlexServer = MagicMock(  # type: ignore[assignment]
        return_value=MagicMock(machineIdentifier="srv-1")
    )
    try:
        await backend.initialize(
            {
                "account_token": "new",
                "server_url": "http://fake",
                "server_machine_id": "srv-1",
            }
        )
    finally:
        mp_mod.MyPlexAccount = mp_orig  # type: ignore[assignment]
        ps_mod.PlexServer = ps_orig  # type: ignore[assignment]

    assert backend._user_tokens == {}
    assert backend._user_servers == {}
    assert backend._user_locks == {}
    assert backend._account_token == "new"


# ── Search filters ────────────────────────────────────────────────


async def test_search_translates_filters_and_normalizes() -> None:
    backend = PlexBackend()
    movie_obj = _load_fixture("movie.xml")

    fake_server = MagicMock()
    fake_server.machineIdentifier = "srv-1"
    fake_server.search.return_value = [movie_obj]
    backend._server = fake_server
    backend._account_token = "tok"

    from gilbert.interfaces.media_library import MediaSearchFilters

    out = await backend.search(
        "dune",
        filters=MediaSearchFilters(kinds=(MediaKind.MOVIE,), limit=5),
    )
    assert len(out) == 1
    assert out[0].kind == MediaKind.MOVIE
    fake_server.search.assert_called_once()
    kwargs = fake_server.search.call_args.kwargs
    assert kwargs.get("mediatype") == "movie"
    assert kwargs.get("limit") == 5


async def test_search_filters_year_range_and_library_section() -> None:
    """Spec §8.8: ``library_section``, ``year_from``, ``year_to``
    translate to a post-fetch filter on the plexapi result list. The
    backend applies the kind / limit kwargs to plexapi and then prunes
    the returned items by year range and library section.
    """
    backend = PlexBackend()
    # Three returned items: one in-range, one too old, one wrong section.
    movie = _load_fixture("movie.xml")  # "Dune: Part Two" (2024) Movies
    # Synthesize two more — same fixture but different year/section.
    too_old = _load_fixture("movie.xml")
    too_old.year = 2009
    wrong_section = _load_fixture("movie.xml")
    wrong_section.librarySectionTitle = "Documentaries"

    fake_server = MagicMock()
    fake_server.machineIdentifier = "srv-1"
    fake_server.search.return_value = [movie, too_old, wrong_section]
    backend._server = fake_server
    backend._account_token = "tok"

    from gilbert.interfaces.media_library import MediaSearchFilters

    out = await backend.search(
        "dune",
        filters=MediaSearchFilters(
            kinds=(MediaKind.MOVIE,),
            library_section="Movies",
            year_from=2010,
            year_to=2030,
            limit=10,
        ),
    )
    assert len(out) == 1
    assert out[0].year == 2024
    assert out[0].library_section == "Movies"


async def test_unauthorized_flips_health_and_evicts_user() -> None:
    """A 401 (Unauthorized) raised by plexapi must translate to
    ``MediaLibraryUnavailableError`` AND evict the per-Home-user cache.
    """
    backend = PlexBackend()
    backend._user_tokens["uA"] = "tA"
    backend._user_servers["uA"] = MagicMock()
    backend._user_locks["uA"] = asyncio.Lock()

    fake_server = MagicMock()
    from plexapi.exceptions import Unauthorized

    def _raise(*a, **kw):
        raise Unauthorized("token revoked")

    fake_server.search = _raise
    backend._user_servers["uA"] = fake_server
    backend._account_token = "tok"
    backend._server = fake_server

    from gilbert.interfaces.media_library import MediaSearchFilters

    with pytest.raises(MediaLibraryUnavailableError):
        await backend.search(
            "x", filters=MediaSearchFilters(), backend_user_id="uA"
        )
    # Per-Home-user cache evicted.
    assert "uA" not in backend._user_tokens
    assert "uA" not in backend._user_servers


# ── list_clients merge with cache (offline re-surface lives at
#   the SERVICE layer; the backend's job is just to return live ones,
#   which we exercise here) ────────────────────────────────────────


async def test_list_clients_merges_account_devices_and_server_clients() -> None:
    backend = PlexBackend()
    backend._account_token = "tok"

    device = _AttrObj(
        clientIdentifier="dev-1",
        provides="player,client",
        name="Living Room TV",
        device="Apple TV",
        platform="tvOS",
    )
    server_client = MagicMock()
    server_client.machineIdentifier = "dev-2"
    server_client.title = "Bedroom TV"
    server_client.product = "Plex Web"
    server_client.platform = "Web"
    server_client.address = "10.0.0.5"

    fake_account = MagicMock()
    fake_account.devices.return_value = [device]
    fake_server = MagicMock()
    fake_server.machineIdentifier = "srv-1"
    fake_server.clients.return_value = [server_client]
    backend._account = fake_account
    backend._server = fake_server

    clients = await backend.list_clients()
    by_id = {c.client_id: c for c in clients}
    assert "dev-1" in by_id
    assert "dev-2" in by_id
    assert by_id["dev-1"].name == "Living Room TV"


# ── play / control / next_episode ─────────────────────────────────


async def test_play_companion_path() -> None:
    backend = PlexBackend()
    backend._account_token = "tok"

    fake_item = MagicMock()
    fake_item.key = "/library/metadata/12345"
    fake_client_obj = MagicMock()
    fake_client_obj.playMedia = MagicMock(return_value=None)

    fake_server = MagicMock()
    fake_server.client.return_value = fake_client_obj
    fake_server.fetchItem.return_value = fake_item
    fake_server.machineIdentifier = "srv-1"
    fake_server.address = "10.0.0.1"
    fake_server.port = 32400
    backend._server = fake_server

    item = MediaItem(
        id="12345",
        backend_name="plex",
        server_id="srv-1",
        title="Dune",
        kind=MediaKind.MOVIE,
    )
    from gilbert.interfaces.media_library import MediaClient

    client = MediaClient(
        client_id="dev-1",
        backend_name="plex",
        server_id="srv-1",
        name="TV",
    )
    cmd = MediaPlayCommand(item=item, client=client, offset_seconds=12.0)
    await backend.play(cmd)
    fake_client_obj.playMedia.assert_called_once_with(
        fake_item, offset=12000
    )


async def test_play_remote_path_uses_clients_playmedia_endpoint() -> None:
    """Spec §8.8: when the companion path (``Client.playMedia``) fails,
    the backend falls through to ``POST /clients/<id>/playMedia``.
    """
    backend = PlexBackend()
    backend._account_token = "tok"
    backend._server_url = "http://plex.local:32400"

    fake_item = MagicMock()
    fake_item.key = "/library/metadata/12345"

    fake_server = MagicMock()
    # Companion path fails — forces the httpx fallback.
    from plexapi.exceptions import PlexApiException

    fake_server.client.side_effect = PlexApiException("offline companion")
    fake_server.fetchItem.return_value = fake_item
    fake_server.machineIdentifier = "srv-1"
    fake_server.address = "10.0.0.1"
    fake_server.port = 32400
    backend._server = fake_server

    # Capture the httpx POST.
    posted: dict[str, Any] = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            pass

    class _FakeHttp:
        async def post(
            self, url: str, *, params: dict[str, Any], headers: dict[str, Any]
        ) -> _FakeResp:
            posted["url"] = url
            posted["params"] = params
            posted["headers"] = headers
            return _FakeResp()

    backend._http = _FakeHttp()  # type: ignore[assignment]

    item = MediaItem(
        id="12345",
        backend_name="plex",
        server_id="srv-1",
        title="Dune",
        kind=MediaKind.MOVIE,
    )
    from gilbert.interfaces.media_library import MediaClient

    client = MediaClient(
        client_id="dev-1",
        backend_name="plex",
        server_id="srv-1",
        name="TV",
    )
    cmd = MediaPlayCommand(item=item, client=client, offset_seconds=12.0)
    await backend.play(cmd)

    # POST /clients/<id>/playMedia with the expected query params.
    assert posted["url"] == "http://plex.local:32400/clients/dev-1/playMedia"
    params = posted["params"]
    assert params["key"] == "/library/metadata/12345"
    assert params["machineIdentifier"] == "srv-1"
    assert params["offset"] == 12000
    # Token rides in the header (not the URL) so log redaction can
    # actually find it.
    assert posted["headers"]["X-Plex-Token"] == "tok"


async def test_next_episode_returns_on_deck() -> None:
    backend = PlexBackend()
    backend._account_token = "tok"

    fake_show = MagicMock()
    fake_episode_obj = _load_fixture("episode.xml")
    fake_show.onDeck = MagicMock(return_value=fake_episode_obj)

    fake_server = MagicMock()
    fake_server.fetchItem.return_value = fake_show
    fake_server.machineIdentifier = "srv-1"
    backend._server = fake_server

    out = await backend.next_episode("22345")
    assert out is not None
    assert out.id == "22399"
    assert out.season_number == 2
    assert out.episode_number == 3


async def test_next_episode_returns_none_when_caught_up() -> None:
    backend = PlexBackend()
    backend._account_token = "tok"

    fake_show = MagicMock()
    fake_show.onDeck = MagicMock(return_value=None)
    # All episodes have viewCount > 0 → caught up.
    ep1 = MagicMock()
    ep1.viewCount = 1
    fake_show.episodes = MagicMock(return_value=[ep1])

    fake_server = MagicMock()
    fake_server.fetchItem.return_value = fake_show
    fake_server.machineIdentifier = "srv-1"
    backend._server = fake_server

    out = await backend.next_episode("22345")
    assert out is None
