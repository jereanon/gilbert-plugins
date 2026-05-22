"""Tests for LutronLights backend with mocked pylutron + bridge."""

from __future__ import annotations

import sys
import types

import pytest
from gilbert_plugin_lutron_radiora import bridge as bridge_module
from gilbert_plugin_lutron_radiora.bridge import reset_shared_bridge
from gilbert_plugin_lutron_radiora.lutron_lights import LutronLights

from gilbert.interfaces.lights import LightsBackend


class _FakeOutput:
    def __init__(self, integration_id: int, name: str, dimmable: bool) -> None:
        self.id = integration_id
        self.name = name
        self.is_dimmable = dimmable
        self.level = 0.0
        self._level = 0.0
        self.set_level_calls: list[float] = []

    def set_level(self, level: float) -> None:
        self._level = level
        self.level = level
        self.set_level_calls.append(level)

    def last_level(self) -> float:
        return self._level


class _FakeShade(_FakeOutput):
    def __init__(self, integration_id: int, name: str) -> None:
        super().__init__(integration_id, name, dimmable=False)

    def stop(self) -> None: ...


class _FakeArea:
    def __init__(self, name: str, outputs: list[_FakeOutput]) -> None:
        self.name = name
        self.outputs = tuple(outputs)


class _FakeLutron:
    def __init__(self, host: str, user: str, password: str) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.name = "Test House"
        kitchen = _FakeArea(
            "Kitchen",
            [_FakeOutput(10, "Kitchen Main", True), _FakeOutput(11, "Pantry Switch", False)],
        )
        outside = _FakeArea("Outside", [_FakeShade(20, "Patio Shade")])
        self.areas = [kitchen, outside]

    def load_xml_db(self, cache_path: str | None = None) -> bool:
        return True

    def connect(self) -> None: ...


@pytest.fixture
def fake_pylutron(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("pylutron")
    fake.Lutron = _FakeLutron  # type: ignore[attr-defined]
    fake.Shade = _FakeShade  # type: ignore[attr-defined]
    fake.Output = _FakeOutput  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pylutron", fake)


@pytest.fixture(autouse=True)
async def _reset() -> None:
    yield
    # Pre-existing issue: each pytest-asyncio test runs in its own event
    # loop, but ``_BRIDGE_LOCK`` is a module-level ``asyncio.Lock`` that
    # binds itself to the loop on first acquire. Resetting it forces the
    # next test's call to ``_lock()`` to create a fresh lock in that
    # test's loop. Previously masked because the warmup ran synchronously
    # inside initialize() and never left state behind; now that warmup
    # is backgrounded, the lock outlives the test loop and trips a
    # ``RuntimeError: bound to a different event loop`` on teardown.
    bridge_module._BRIDGE_LOCK = None
    await reset_shared_bridge()


def test_registered_under_correct_name() -> None:
    backends = LightsBackend.registered_backends()
    assert backends.get("lutron-radiora") is LutronLights
    assert LutronLights.supports_dimming is True


def test_backend_config_params_have_sensitive_password() -> None:
    params = {p.key: p for p in LutronLights.backend_config_params()}
    assert {"host", "username", "password"} <= set(params)
    assert params["password"].sensitive is True


async def test_initialize_connects_bridge(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize(
        {"host": "10.0.0.1", "username": "u", "password": "p"}
    )
    # initialize() backgrounds the telnet handshake. Await the warmup
    # task so the assertion sees the populated bridge.
    assert backend._warmup_task is not None
    await backend._warmup_task
    assert bridge_module._BRIDGE is not None


async def test_list_lights_returns_only_non_shade_outputs(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    lights = await backend.list_lights()
    names = {light.name for light in lights}
    assert names == {"Kitchen Main", "Pantry Switch"}
    # Per-device dimming flag follows pylutron's is_dimmable.
    by_name = {light.name: light for light in lights}
    assert by_name["Kitchen Main"].supports_dimming is True
    assert by_name["Pantry Switch"].supports_dimming is False
    # Area populated.
    assert by_name["Kitchen Main"].area == "Kitchen"


async def test_set_level_round_trips_through_bridge(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    await backend.set_level("10", 60.0)
    bridge = bridge_module._BRIDGE
    assert bridge is not None
    output = bridge.light_by_id("10")
    assert output.set_level_calls == [60.0]


async def test_set_level_clamps_out_of_range(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    await backend.set_level("10", 250.0)
    output = bridge_module._BRIDGE.light_by_id("10")
    assert output.set_level_calls == [100.0]


async def test_set_level_unknown_id_raises(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    with pytest.raises(KeyError):
        await backend.set_level("999", 50.0)


async def test_test_connection_action_reports_counts(fake_pylutron: None) -> None:
    backend = LutronLights()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "ok"
    assert "2 lights" in result.message
    assert "1 shades" in result.message


async def test_test_connection_action_unconfigured_returns_error() -> None:
    backend = LutronLights()
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "error"
