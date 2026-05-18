"""Tests for NexiaThermostatBackend with mocked nexia + aiohttp.

The real ``nexia`` library talks to Nexia's cloud over HTTP, so every
test injects a fake ``nexia.home`` module exposing a ``NexiaHome``
class with the methods the backend actually calls.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from gilbert_plugin_american_standard.nexia_backend import (
    NexiaThermostatBackend,
    _split_id,
)

from gilbert.interfaces.thermostat import ThermostatBackend


class _FakeZone:
    def __init__(
        self,
        zone_id: int,
        name: str,
        *,
        temp: float,
        heat: float,
        cool: float,
        mode: str = "AUTO",
    ) -> None:
        self.zone_id = zone_id
        self._name = name
        self._temp = temp
        self._heat = heat
        self._cool = cool
        self._mode = mode
        self.set_heat_cool_calls: list[tuple[float | None, float | None]] = []
        self.set_mode_calls: list[str] = []

    def get_name(self) -> str:
        return self._name

    def get_temperature(self) -> float:
        return self._temp

    def get_heating_setpoint(self) -> float:
        return self._heat

    def get_cooling_setpoint(self) -> float:
        return self._cool

    def get_current_mode(self) -> str:
        return self._mode

    async def set_heat_cool_temp(
        self,
        heat_temperature: float | None = None,
        cool_temperature: float | None = None,
        set_temperature: float | None = None,
    ) -> None:
        self.set_heat_cool_calls.append((heat_temperature, cool_temperature))
        if heat_temperature is not None:
            self._heat = heat_temperature
        if cool_temperature is not None:
            self._cool = cool_temperature

    async def set_mode(self, mode: str) -> None:
        self.set_mode_calls.append(mode)
        self._mode = mode


class _FakeThermostat:
    def __init__(
        self,
        thermostat_id: int,
        name: str,
        zones: list[_FakeZone],
        *,
        unit: str = "F",
        humidity: float | None = None,
        fan_mode: str | None = "auto",
    ) -> None:
        self.thermostat_id = thermostat_id
        self._name = name
        self._zones = {z.zone_id: z for z in zones}
        self._unit = unit
        self._humidity = humidity
        self._fan_mode = fan_mode
        self.set_fan_mode_calls: list[str] = []

    def get_name(self) -> str:
        return self._name

    def get_unit(self) -> str:
        return self._unit

    def has_relative_humidity(self) -> bool:
        return self._humidity is not None

    def get_relative_humidity(self) -> float | None:
        return self._humidity

    def get_fan_mode(self) -> str | None:
        return self._fan_mode

    def get_fan_modes(self) -> list[str]:
        return ["auto", "on", "circulate"]

    async def set_fan_mode(self, fan_mode: str) -> None:
        self.set_fan_mode_calls.append(fan_mode)
        self._fan_mode = fan_mode

    def get_zone_ids(self) -> list[int]:
        return list(self._zones)

    def get_zone_by_id(self, zone_id: Any) -> _FakeZone:
        return self._zones[int(zone_id)]


class _FakeHome:
    last_init: dict[str, Any] = {}
    login_called: int = 0
    update_called: int = 0

    def __init__(
        self,
        session: Any,
        *,
        username: str = "",
        password: str = "",
        brand: str = "nexia",
        state_file: str | None = None,
        **_: Any,
    ) -> None:
        type(self).last_init = {
            "session": session,
            "username": username,
            "password": password,
            "brand": brand,
            "state_file": state_file,
        }
        zone_a = _FakeZone(1, "Upstairs", temp=70, heat=68, cool=76, mode="HEAT")
        zone_b = _FakeZone(2, "Downstairs", temp=72, heat=66, cool=78, mode="AUTO")
        therm = _FakeThermostat(
            10,
            "Main HVAC",
            [zone_a, zone_b],
            humidity=42.0,
            fan_mode="auto",
        )
        self._thermostats = {therm.thermostat_id: therm}

    async def login(self) -> None:
        type(self).login_called += 1

    async def update(self, force_update: bool = True) -> None:
        type(self).update_called += 1

    def get_thermostat_ids(self) -> list[int]:
        return list(self._thermostats)

    def get_thermostat_by_id(self, thermostat_id: Any) -> _FakeThermostat:
        return self._thermostats[int(thermostat_id)]


class _FakeSession:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_nexia(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHome]:
    # Reset class-level counters so each test starts clean.
    _FakeHome.login_called = 0
    _FakeHome.update_called = 0
    _FakeHome.last_init = {}

    home_module = types.ModuleType("nexia.home")
    home_module.NexiaHome = _FakeHome  # type: ignore[attr-defined]
    nexia_pkg = types.ModuleType("nexia")
    nexia_pkg.home = home_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nexia", nexia_pkg)
    monkeypatch.setitem(sys.modules, "nexia.home", home_module)

    # Don't talk to the real network even if aiohttp is imported.
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)

    return _FakeHome


# --- Registration ---


def test_registered_under_correct_name() -> None:
    backends = ThermostatBackend.registered_backends()
    assert backends.get("american-standard") is NexiaThermostatBackend
    assert NexiaThermostatBackend.supports_cooling is True
    assert NexiaThermostatBackend.supports_heating is True
    assert NexiaThermostatBackend.supports_fan_mode is True


def test_backend_config_params_have_sensitive_password() -> None:
    params = {p.key: p for p in NexiaThermostatBackend.backend_config_params()}
    assert {"username", "password", "brand"} == set(params)
    assert params["password"].sensitive is True
    assert "nexia" in (params["brand"].choices or ())


def test_split_id_round_trip() -> None:
    assert _split_id("10:1") == ("10", "1")
    with pytest.raises(KeyError):
        _split_id("malformed")


# --- Lifecycle ---


async def test_initialize_without_credentials_is_noop(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({})
    assert backend._home is None
    assert _FakeHome.login_called == 0


async def test_initialize_with_credentials_logs_in(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p", "brand": "nexia"})
    # initialize() now backgrounds the connect so Gilbert startup isn't
    # blocked behind the Nexia cloud login. Await the warmup task
    # before asserting state.
    assert backend._warmup_task is not None
    await backend._warmup_task
    assert backend._home is not None
    assert _FakeHome.login_called == 1
    # update() is called once during initialize() too, to populate state.
    assert _FakeHome.update_called >= 1
    assert _FakeHome.last_init["username"] == "u@x"
    assert _FakeHome.last_init["brand"] == "nexia"


async def test_close_releases_session(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    # Wait for the backgrounded connect so ``_session`` is populated.
    assert backend._warmup_task is not None
    await backend._warmup_task
    session = backend._session
    await backend.close()
    assert backend._home is None
    assert backend._session is None
    assert session is not None and session.closed


# --- Listing & status ---


async def test_list_thermostats_returns_one_info_per_zone(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    items = await backend.list_thermostats()
    assert len(items) == 2
    by_name = {item.name: item for item in items}
    assert {"Upstairs", "Downstairs"} == set(by_name)
    upstairs = by_name["Upstairs"]
    assert upstairs.thermostat_id == "10:1"
    # Area = thermostat (gateway) name.
    assert upstairs.area == "Main HVAC"
    assert upstairs.current_temperature == 70
    assert upstairs.heat_setpoint == 68
    assert upstairs.cool_setpoint == 76
    # Mode "HEAT" -> "heat"
    assert upstairs.mode == "heat"
    # Humidity propagated from the thermostat.
    assert upstairs.current_humidity == 42.0
    assert upstairs.has_humidity_sensor is True
    assert upstairs.temperature_unit == "F"
    assert upstairs.fan_mode == "auto"


async def test_humidity_normalized_when_reported_as_fraction(
    fake_nexia: type[_FakeHome],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some nexia firmwares report humidity as 0..1 — make sure we
    # convert that to a percentage rather than reporting "0.42% RH".
    monkeypatch.setattr(_FakeThermostat, "get_relative_humidity", lambda self: 0.42)
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    items = await backend.list_thermostats()
    assert items[0].current_humidity == pytest.approx(42.0)


async def test_get_status_refreshes_and_returns_zone(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    info = await backend.get_status("10:2")
    assert info.name == "Downstairs"
    assert info.mode == "auto"


# --- Control ---


async def test_set_setpoint_heat_only(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    await backend.set_setpoint("10:1", heat=70)
    therm = backend._home.get_thermostat_by_id(10)
    zone = therm.get_zone_by_id(1)
    assert zone.set_heat_cool_calls == [(70, None)]


async def test_set_setpoint_both(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    await backend.set_setpoint("10:1", heat=66, cool=78)
    zone = backend._home.get_thermostat_by_id(10).get_zone_by_id(1)
    assert zone.set_heat_cool_calls == [(66, 78)]


async def test_set_mode_translates_to_uppercase(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    await backend.set_mode("10:1", "cool")
    zone = backend._home.get_thermostat_by_id(10).get_zone_by_id(1)
    assert zone.set_mode_calls == ["COOL"]


async def test_set_mode_rejects_unknown_mode(fake_nexia: type[_FakeHome]) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    with pytest.raises(ValueError):
        await backend.set_mode("10:1", "dehumidify")


async def test_set_fan_mode_dispatches_to_thermostat(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    await backend.set_fan_mode("10:1", "circulate")
    therm = backend._home.get_thermostat_by_id(10)
    assert therm.set_fan_mode_calls == ["circulate"]


async def test_set_fan_mode_rejects_unsupported_label(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    with pytest.raises(ValueError):
        await backend.set_fan_mode("10:1", "turbo")


# --- Test connection action ---


async def test_test_connection_action_reports_counts(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "ok"
    assert "1 thermostat" in result.message
    assert "2 zone" in result.message


async def test_test_connection_action_unconfigured_returns_error() -> None:
    backend = NexiaThermostatBackend()
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "error"


async def test_test_connection_action_unknown_key(
    fake_nexia: type[_FakeHome],
) -> None:
    backend = NexiaThermostatBackend()
    await backend.initialize({"username": "u@x", "password": "p"})
    result = await backend.invoke_backend_action("nonexistent", {})
    assert result.status == "error"
