"""Tests for the UniFi doorbell backend — Protect doorbells + Access readers."""

from unittest.mock import AsyncMock

import pytest
from gilbert_plugin_unifi.access import (
    AccessDoor,
    BadgeEvent,
    UniFiAccess,
    _looks_like_doorbell,
)
from gilbert_plugin_unifi.client import UniFiClient
from gilbert_plugin_unifi.doorbell import UniFiProtectDoorbellBackend
from gilbert_plugin_unifi.protect import Camera, UniFiProtect


def _camera(
    name: str,
    *,
    feature_flags: dict | None = None,
    camera_type: str = "",
    market_name: str = "",
) -> dict:
    return {
        "id": f"id-{name}",
        "name": name,
        "type": camera_type,
        "marketName": market_name,
        "state": "CONNECTED",
        "lastMotion": 0,
        "featureFlags": feature_flags or {},
    }


# =============================================================================
# Protect doorbell heuristic
# =============================================================================


class TestProtectDoorbellDetection:
    @pytest.mark.asyncio
    async def test_has_chime_flag(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Front", feature_flags={"hasChime": True})]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is True

    @pytest.mark.asyncio
    async def test_is_doorbell_flag(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Side", feature_flags={"isDoorbell": True})]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is True

    @pytest.mark.asyncio
    async def test_has_button_flag(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Back", feature_flags={"hasButton": True})]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is True

    @pytest.mark.asyncio
    async def test_doorbell_in_type_field(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Front", camera_type="UVC G4 Doorbell Pro")]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is True

    @pytest.mark.asyncio
    async def test_doorbell_in_market_name(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Front", market_name="G5 Doorbell")]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is True

    @pytest.mark.asyncio
    async def test_plain_camera_not_doorbell(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [_camera("Driveway", camera_type="UVC G4 Bullet")]
        cameras = await UniFiProtect(client).list_cameras()
        assert cameras[0].is_doorbell is False


# =============================================================================
# Access door listing
# =============================================================================


class TestAccessListDoors:
    @pytest.mark.asyncio
    async def test_extracts_named_doors(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = {
            "data": [
                {"id": "1", "name": "front pedestrian gate", "type": "UA-Pro"},
                {"id": "2", "alias": "garage", "type": "UA-Hub"},
            ]
        }
        doors = await UniFiAccess(client).list_doors()
        assert [d.name for d in doors] == ["front pedestrian gate", "garage"]
        assert doors[0].model == "UA-Pro"

    @pytest.mark.asyncio
    async def test_skips_unnamed(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = {"data": [{"id": "1"}]}
        assert await UniFiAccess(client).list_doors() == []

    @pytest.mark.asyncio
    async def test_handles_list_response(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = [{"id": "1", "name": "side"}]
        doors = await UniFiAccess(client).list_doors()
        assert doors[0].name == "side"

    @pytest.mark.asyncio
    async def test_none_response(self) -> None:
        client = AsyncMock(spec=UniFiClient)
        client.get.return_value = None
        assert await UniFiAccess(client).list_doors() == []


class TestDoorbellEventFilter:
    def test_doorbell_keyword(self) -> None:
        assert _looks_like_doorbell("access.doorbell.press") is True

    def test_ring_keyword(self) -> None:
        assert _looks_like_doorbell("doorbell.ring") is True

    def test_intercom_keyword(self) -> None:
        assert _looks_like_doorbell("intercom.call") is True

    def test_routine_unlock_skipped(self) -> None:
        assert _looks_like_doorbell("access.unlock_via_card") is False


# =============================================================================
# Combined doorbell backend
# =============================================================================


@pytest.fixture
def backend() -> UniFiProtectDoorbellBackend:
    b = UniFiProtectDoorbellBackend()
    b._protect = AsyncMock(spec=UniFiProtect)
    b._access = AsyncMock(spec=UniFiAccess)
    return b


class TestDoorbellBackendListing:
    @pytest.mark.asyncio
    async def test_merges_protect_and_access(self, backend: UniFiProtectDoorbellBackend) -> None:
        backend._protect.list_cameras.return_value = [
            Camera("c1", "Front Door", "G4 Doorbell", "CONNECTED", 0, is_doorbell=True),
            Camera("c2", "Driveway", "G4 Bullet", "CONNECTED", 0, is_doorbell=False),
        ]
        backend._access.list_doors.return_value = [
            AccessDoor("d1", "front pedestrian gate", "UA-Pro", True),
        ]
        names = await backend.list_doorbell_names()
        assert names == ["Front Door", "front pedestrian gate"]

    @pytest.mark.asyncio
    async def test_dedupes_case_insensitive(self, backend: UniFiProtectDoorbellBackend) -> None:
        backend._protect.list_cameras.return_value = [
            Camera("c1", "Front Door", "G4 Doorbell", "CONNECTED", 0, is_doorbell=True),
        ]
        backend._access.list_doors.return_value = [
            AccessDoor("d1", "front door", "UA-Pro", True),
        ]
        names = await backend.list_doorbell_names()
        assert names == ["Front Door"]

    @pytest.mark.asyncio
    async def test_only_access_configured(self, backend: UniFiProtectDoorbellBackend) -> None:
        backend._protect = None
        backend._access.list_doors.return_value = [
            AccessDoor("d1", "front pedestrian gate", "UA-Pro", True),
        ]
        names = await backend.list_doorbell_names()
        assert names == ["front pedestrian gate"]


class TestDoorbellBackendRingEvents:
    @pytest.mark.asyncio
    async def test_includes_access_doorbell_events(
        self, backend: UniFiProtectDoorbellBackend
    ) -> None:
        backend._protect.get_detection_events.return_value = []
        backend._access.get_doorbell_events.return_value = [
            BadgeEvent(
                event_id="e1",
                person_name="",
                direction="in",
                door_name="front pedestrian gate",
                timestamp=12345,
                event_type="access.doorbell.press",
            )
        ]
        events = await backend.get_ring_events(lookback_seconds=10)
        assert len(events) == 1
        assert events[0].camera_name == "front pedestrian gate"
        assert events[0].timestamp == 12345

    @pytest.mark.asyncio
    async def test_protect_failure_does_not_block_access(
        self, backend: UniFiProtectDoorbellBackend
    ) -> None:
        from gilbert_plugin_unifi.client import UniFiAPIError

        backend._protect.get_detection_events.side_effect = UniFiAPIError("boom")
        backend._access.get_doorbell_events.return_value = [
            BadgeEvent(
                event_id="e1",
                person_name="",
                direction="in",
                door_name="front pedestrian gate",
                timestamp=12345,
                event_type="doorbell.ring",
            )
        ]
        events = await backend.get_ring_events(lookback_seconds=10)
        assert [e.camera_name for e in events] == ["front pedestrian gate"]
