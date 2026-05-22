"""Service / tool / WS handler tests with mocked speaker and scheduler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert_plugin_andon_fm.andon_fm_service import AndonFmService
from gilbert_plugin_andon_fm.scraper import CurrentBlock, NowPlayingSnapshot
from gilbert_plugin_andon_fm.stations import BUNDLED_STATIONS


class _FakeSpeakerInfo:
    """Stand-in for ``gilbert.interfaces.speaker.SpeakerInfo``.

    Tests don't import the real dataclass to keep the plugin tests
    decoupled from core internals — the speakers.list WS handler
    reads ``speaker_id`` / ``name`` / ``model`` / ``backend_name`` /
    ``group_name`` via ``getattr``, so a duck-typed stub is sufficient.
    ``speaker_id`` matters because the handler relabels the caller's
    own browser entry to "My browser tab" when it sees
    ``speaker_id == "browser:<caller_user_id>"``.
    """

    def __init__(
        self,
        name: str,
        speaker_id: str = "",
        model: str = "",
        backend_name: str = "",
        group_name: str = "",
    ) -> None:
        self.name = name
        self.speaker_id = speaker_id
        self.model = model
        self.backend_name = backend_name
        self.group_name = group_name


class _FakeSpeakerSvc:
    """Stub that satisfies the ``SpeakerLister`` protocol via an
    async ``list_speakers`` method — the speakers.list WS handler
    asks for a fresh live list each time the picker opens."""

    def __init__(self) -> None:
        self.play_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []
        self.speakers: list[_FakeSpeakerInfo] = []

    async def play_on_speakers(self, **kwargs: Any) -> None:
        self.play_calls.append(kwargs)

    async def stop_speakers(self, **kwargs: Any) -> None:
        self.stop_calls.append(kwargs)

    async def list_speakers(self) -> list[_FakeSpeakerInfo]:
        return list(self.speakers)


class _FakeResolver:
    def __init__(self, speaker_svc: _FakeSpeakerSvc) -> None:
        self._speaker = speaker_svc

    def get_capability(self, name: str) -> Any:
        if name == "configuration":
            return None  # accept defaults
        if name == "scheduler":
            return None  # no scheduler — scraper won't be wired
        if name == "event_bus":
            return None
        return None

    def require_capability(self, name: str) -> Any:
        if name == "speaker_control":
            return self._speaker
        raise KeyError(name)


@pytest.fixture
async def started_service() -> Any:
    svc = AndonFmService()
    speaker = _FakeSpeakerSvc()
    await svc.start(_FakeResolver(speaker))
    yield svc, speaker
    await svc.stop()


@pytest.mark.asyncio
async def test_get_tools_lists_four_when_enabled(started_service: Any) -> None:
    svc, _ = started_service
    tools = svc.get_tools()
    names = {t.name for t in tools}
    assert names == {
        "andon_list_stations",
        "andon_play_station",
        "andon_stop_station",
        "andon_now_playing",
    }
    # Slash commands are namespaced under "radio"
    assert svc.slash_namespace == "radio"
    for t in tools:
        assert t.slash_command, f"{t.name} missing slash_command"
        assert t.slash_help, f"{t.name} missing slash_help"


@pytest.mark.asyncio
async def test_play_station_routes_to_speaker_service(started_service: Any) -> None:
    svc, speaker = started_service
    result = await svc.execute_tool(
        "andon_play_station",
        {"station": "OpenAIR", "speakers": ["my browser"], "volume": 40},
    )
    assert "OpenAIR" in result
    assert len(speaker.play_calls) == 1
    call = speaker.play_calls[0]
    assert call["uri"] == "https://streaming.live365.com/a81044"
    assert call["speaker_names"] == ["my browser"]
    assert call["volume"] == 40
    assert call["kind"] == "andon_fm"
    assert "OpenAIR" in call["title"]


@pytest.mark.asyncio
async def test_play_station_uses_defaults_when_missing(started_service: Any) -> None:
    svc, speaker = started_service
    await svc.execute_tool("andon_play_station", {"station": "Claude"})
    call = speaker.play_calls[0]
    # Default config sets speakers=["my browser"], volume=60
    assert call["speaker_names"] == ["my browser"]
    assert call["volume"] == 60
    # "Claude" host fell back to Thinking Frequencies
    assert call["uri"] == "https://streaming.live365.com/a46431"


@pytest.mark.asyncio
async def test_play_unknown_station_returns_error_message(started_service: Any) -> None:
    svc, speaker = started_service
    result = await svc.execute_tool("andon_play_station", {"station": "WKRP"})
    assert "Unknown" in result
    assert len(speaker.play_calls) == 0


@pytest.mark.asyncio
async def test_stop_calls_speaker_service(started_service: Any) -> None:
    svc, speaker = started_service
    result = await svc.execute_tool(
        "andon_stop_station", {"speakers": ["kitchen"]}
    )
    assert "Stopped" in result
    assert speaker.stop_calls[0]["speaker_names"] == ["kitchen"]


@pytest.mark.asyncio
async def test_now_playing_reports_no_data_initially(started_service: Any) -> None:
    svc, _ = started_service
    out = await svc.execute_tool("andon_now_playing", {})
    assert "No now-playing data" in out


@pytest.mark.asyncio
async def test_now_playing_with_cache_renders_block(started_service: Any) -> None:
    svc, _ = started_service
    s = BUNDLED_STATIONS[0]
    svc._now_playing[s.id] = NowPlayingSnapshot(
        station_id=s.id,
        block=CurrentBlock(
            name="Late Night Lounge", description="Chill grooves.", duration_minutes=60
        ),
        fetched_at=1000.0,
        listeners=15,
    )
    out = await svc.execute_tool("andon_now_playing", {"station": s.name})
    assert "Late Night Lounge" in out
    assert "15 listening" in out
    assert "Chill grooves" in out


@pytest.mark.asyncio
async def test_list_stations_includes_all_four(started_service: Any) -> None:
    svc, _ = started_service
    out = await svc.execute_tool("andon_list_stations", {})
    for s in BUNDLED_STATIONS:
        assert s.name in out
        assert s.host in out


@pytest.mark.asyncio
async def test_ws_stations_list_returns_catalog(started_service: Any) -> None:
    svc, _ = started_service
    resp = await svc._ws_stations_list(conn=None, frame={"id": "req-1"})
    assert resp["type"] == "andon_fm.stations.list.result"
    assert resp["ref"] == "req-1"
    assert len(resp["stations"]) == 4
    sample = resp["stations"][0]
    assert "stream_url" in sample
    assert "image_url" in sample
    assert sample["stale"] is True  # no fetch has run
    assert resp["defaults"]["speakers"] == ["my browser"]


@pytest.mark.asyncio
async def test_ws_speakers_list_returns_live_speakers(
    started_service: Any,
) -> None:
    """The picker RPC returns whatever the speaker service's live
    ``list_speakers()`` reports — no synthetic / virtual entries,
    so a user with no enabled browser tab and no Sonos sees an
    empty list rather than a misleading "this browser tab" row."""
    svc, speaker = started_service
    speaker.speakers = [
        _FakeSpeakerInfo(
            name="Kitchen",
            speaker_id="sonos:RINCON_kitchen",
            model="Sonos One",
            backend_name="sonos",
            group_name="Downstairs",
        ),
        _FakeSpeakerInfo(
            name="Office",
            speaker_id="local:office",
            backend_name="local",
        ),
        _FakeSpeakerInfo(
            name="Someone Else's Browser",
            speaker_id="browser:other_user",
            backend_name="browser",
        ),
    ]
    resp = await svc._ws_speakers_list(conn=None, frame={"id": "s-1"})

    assert resp["type"] == "andon_fm.speakers.list.result"
    assert resp["ref"] == "s-1"
    assert resp["defaults"]["speakers"] == ["my browser"]

    ids = [s["id"] for s in resp["speakers"]]
    assert ids == ["Kitchen", "Office", "Someone Else's Browser"]

    kitchen = resp["speakers"][0]
    assert kitchen["backend"] == "sonos"
    assert kitchen["model"] == "Sonos One"
    assert kitchen["group_name"] == "Downstairs"

    other_browser = resp["speakers"][2]
    assert other_browser["backend"] == "browser"
    # Not relabeled — this entry isn't the caller's own browser
    # (there's no current-user context in this test, so user_id is
    # empty and the "is_self_browser" relabel doesn't fire).
    assert other_browser["name"] == "Someone Else's Browser"


@pytest.mark.asyncio
async def test_ws_speakers_list_relabels_callers_own_browser(
    started_service: Any,
) -> None:
    """When the caller's own ``browser:<user_id>`` entry shows up in
    the live speakers list, the picker relabels it ``My browser tab``
    (with id ``"my browser"``) so the dialog reads naturally and the
    play request flows through the speaker service's caller-aware
    magic alias rather than a possibly-non-unique display name."""
    from gilbert.interfaces.auth import UserContext
    from gilbert.interfaces.context import set_current_user

    svc, speaker = started_service
    speaker.speakers = [
        _FakeSpeakerInfo(
            name="Kitchen",
            speaker_id="sonos:RINCON_kitchen",
            backend_name="sonos",
        ),
        _FakeSpeakerInfo(
            name="Brian Dilley's Browser",
            speaker_id="browser:vendors",
            backend_name="browser",
        ),
        _FakeSpeakerInfo(
            name="Alice's Browser",
            speaker_id="browser:alice",
            backend_name="browser",
        ),
    ]

    set_current_user(
        UserContext(
            user_id="vendors",
            email="vendors@current-la.com",
            display_name="Brian Dilley",
        )
    )

    resp = await svc._ws_speakers_list(conn=None, frame={"id": "s-self"})

    by_id = {s["id"]: s for s in resp["speakers"]}
    assert "my browser" in by_id, "Caller's own browser should be relabeled"
    assert by_id["my browser"]["name"] == "My browser tab"
    assert by_id["my browser"]["backend"] == "browser"
    # Other users' browsers retain their display name + id so admins
    # can still tell them apart.
    assert by_id["Alice's Browser"]["name"] == "Alice's Browser"
    assert "Kitchen" in by_id


@pytest.mark.asyncio
async def test_ws_speakers_list_returns_empty_when_no_speakers(
    started_service: Any,
) -> None:
    """No registered speakers ⇒ empty payload. The picker dialog
    treats this as "enable a speaker backend" — no synthetic
    fallback row to mislead the user."""
    svc, _ = started_service
    resp = await svc._ws_speakers_list(conn=None, frame={"id": "s-empty"})
    assert resp["speakers"] == []


@pytest.mark.asyncio
async def test_ws_speakers_list_survives_live_fetch_failure(
    started_service: Any,
) -> None:
    """An exception inside ``list_speakers()`` leaves the picker
    rendering an empty list — never 500ing the dialog."""
    svc, _ = started_service

    class _ExplodingSvc:
        async def list_speakers(self) -> list[Any]:
            raise RuntimeError("backend down")

    svc._speaker_svc = _ExplodingSvc()
    resp = await svc._ws_speakers_list(conn=None, frame={"id": "s-2"})
    assert resp["speakers"] == []


@pytest.mark.asyncio
async def test_ws_speakers_list_handles_non_protocol_service(
    started_service: Any,
) -> None:
    """If the resolved speaker service doesn't satisfy
    ``SpeakerLister`` (no ``list_speakers``), the picker still
    returns an empty list rather than crashing."""
    svc, _ = started_service

    class _BareSvc:
        async def play_on_speakers(self, **kwargs: Any) -> None:
            return None

    svc._speaker_svc = _BareSvc()
    resp = await svc._ws_speakers_list(conn=None, frame={"id": "s-3"})
    assert resp["speakers"] == []


@pytest.mark.asyncio
async def test_ws_play_dispatches_to_speaker(started_service: Any) -> None:
    svc, speaker = started_service
    resp = await svc._ws_play(
        conn=None,
        frame={
            "id": "req-2",
            "station": "Backlink Broadcast",
            "speakers": ["my browser"],
            "volume": 55,
        },
    )
    assert resp["ok"] is True
    assert resp["station_id"] == "aab4d149-92fa-4386-9c1e-d938ecb66ee3"
    assert speaker.play_calls[0]["uri"] == "https://streaming.live365.com/a13541"


@pytest.mark.asyncio
async def test_ws_play_unknown_station_returns_error(started_service: Any) -> None:
    svc, speaker = started_service
    resp = await svc._ws_play(conn=None, frame={"id": "x", "station": "noop"})
    assert resp["ok"] is False
    assert "Unknown" in resp["error"]
    assert speaker.play_calls == []


@pytest.mark.asyncio
async def test_refresh_now_playing_publishes_change_events() -> None:
    """When a station's block name changes, a single event is emitted."""

    published: list[Any] = []

    class _Bus:
        async def publish(self, event: Any) -> None:
            published.append(event)

    class _BusProvider:
        @property
        def bus(self) -> Any:
            return _Bus()

    class _Resolver(_FakeResolver):
        def get_capability(self, name: str) -> Any:
            if name == "event_bus":
                return _BusProvider()
            return super().get_capability(name)

    svc = AndonFmService()
    svc._scraper_enabled = False  # don't touch the scheduler
    await svc.start(_Resolver(_FakeSpeakerSvc()))

    # Mock the underlying fetch to deliver two distinct snapshots.
    from gilbert_plugin_andon_fm import andon_fm_service as svc_mod

    async def first(*_a: Any, **_kw: Any) -> dict[str, Any]:
        s = BUNDLED_STATIONS[0]
        return {
            s.id: NowPlayingSnapshot(
                station_id=s.id,
                block=CurrentBlock(name="Block A"),
                fetched_at=0.0,
                listeners=1,
            )
        }

    async def second(*_a: Any, **_kw: Any) -> dict[str, Any]:
        s = BUNDLED_STATIONS[0]
        return {
            s.id: NowPlayingSnapshot(
                station_id=s.id,
                block=CurrentBlock(name="Block B"),
                fetched_at=0.0,
                listeners=2,
            )
        }

    svc_mod.fetch_now_playing = first  # type: ignore[assignment]
    await svc._refresh_now_playing()
    assert len(published) == 1
    assert published[0].event_type == "andon_fm.now_playing.changed"
    assert published[0].data["block"]["name"] == "Block A"

    svc_mod.fetch_now_playing = second  # type: ignore[assignment]
    await svc._refresh_now_playing()
    assert len(published) == 2
    assert published[1].data["block"]["name"] == "Block B"

    # Same block again → no new event
    await svc._refresh_now_playing()
    assert len(published) == 2

    await svc.stop()
