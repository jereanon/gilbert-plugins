"""Tests for LutronShades backend with mocked pylutron + bridge."""

from __future__ import annotations

import sys
import types

import pytest
from gilbert_plugin_lutron_radiora import bridge as bridge_module
from gilbert_plugin_lutron_radiora.bridge import reset_shared_bridge
from gilbert_plugin_lutron_radiora.lutron_shades import LutronShades

from gilbert.interfaces.shades import ShadesBackend


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
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


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
        bedroom = _FakeArea("Bedroom", [_FakeShade(50, "Bedroom Shade")])
        living = _FakeArea(
            "Living Room",
            [
                _FakeShade(51, "South Window"),
                _FakeOutput(52, "Reading Lamp", True),
            ],
        )
        self.areas = [bedroom, living]

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
    # See test_lutron_lights.py::_reset for the rationale on resetting
    # the module-level ``_BRIDGE_LOCK`` between tests.
    bridge_module._BRIDGE_LOCK = None
    await reset_shared_bridge()


def test_registered_under_correct_name() -> None:
    backends = ShadesBackend.registered_backends()
    assert backends.get("lutron-radiora") is LutronShades
    assert LutronShades.supports_position is True
    assert LutronShades.supports_stop is True


async def test_list_shades_returns_only_shade_outputs(fake_pylutron: None) -> None:
    backend = LutronShades()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    shades = await backend.list_shades()
    assert {s.name for s in shades} == {"Bedroom Shade", "South Window"}
    by_name = {s.name: s for s in shades}
    assert by_name["Bedroom Shade"].area == "Bedroom"


async def test_set_position_round_trips(fake_pylutron: None) -> None:
    backend = LutronShades()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    await backend.set_position("50", 30.0)
    shade = bridge_module._BRIDGE.shade_by_id("50")
    assert shade.set_level_calls == [30.0]


async def test_stop_invokes_shade_stop(fake_pylutron: None) -> None:
    backend = LutronShades()
    await backend.initialize({"host": "10.0.0.1", "username": "u", "password": "p"})
    await backend.stop("50")
    shade = bridge_module._BRIDGE.shade_by_id("50")
    assert shade.stop_calls == 1


async def test_shared_bridge_with_lights(fake_pylutron: None) -> None:
    """Initializing both backends with the same credentials must reuse one bridge."""
    from gilbert_plugin_lutron_radiora.lutron_lights import LutronLights

    lights = LutronLights()
    shades = LutronShades()
    cfg = {"host": "10.0.0.1", "username": "u", "password": "p"}
    await lights.initialize(cfg)
    # initialize() backgrounds the connect — wait for both warmups so
    # the shared bridge is fully constructed before we sample it.
    assert lights._warmup_task is not None
    await lights._warmup_task
    bridge_after_lights = bridge_module._BRIDGE
    await shades.initialize(cfg)
    assert shades._warmup_task is not None
    await shades._warmup_task
    bridge_after_shades = bridge_module._BRIDGE
    assert bridge_after_lights is bridge_after_shades
