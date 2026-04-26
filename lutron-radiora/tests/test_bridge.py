"""Tests for LutronBridge with a mocked pylutron module."""

from __future__ import annotations

import sys
import types

import pytest
from gilbert_plugin_lutron_radiora.bridge import (
    LutronBridge,
    reset_shared_bridge,
    shared_bridge,
)


class _FakeOutput:
    def __init__(self, integration_id: int, name: str, is_dimmable: bool = True) -> None:
        self.id = integration_id
        self.name = name
        self.is_dimmable = is_dimmable
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
        super().__init__(integration_id, name, is_dimmable=False)
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeArea:
    def __init__(self, name: str, outputs: list[_FakeOutput]) -> None:
        self.name = name
        self.outputs = tuple(outputs)


class _FakeLutron:
    """Mimics enough of pylutron.Lutron to drive LutronBridge."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        areas: list[_FakeArea] | None = None,
        project_name: str = "Test House",
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.areas = areas or []
        self.name = project_name
        self.connected = False
        self.xml_loaded = False

    def load_xml_db(self, cache_path: str | None = None) -> bool:
        self.xml_loaded = True
        return True

    def connect(self) -> None:
        self.connected = True


@pytest.fixture
def fake_pylutron(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Inject a fake ``pylutron`` module into sys.modules.

    LutronBridge does ``import pylutron`` lazily inside ``_sync_connect``,
    and uses ``isinstance(output, pylutron.Shade)`` for type discrimination.
    The fake module exposes a ``Shade`` class set to ``_FakeShade`` so
    those isinstance checks work.
    """
    fake = types.ModuleType("pylutron")

    living = _FakeArea(
        "Living Room",
        [_FakeOutput(1, "Sofa Lamp", is_dimmable=True)],
    )
    bedroom = _FakeArea(
        "Bedroom",
        [
            _FakeOutput(2, "Ceiling", is_dimmable=True),
            _FakeShade(3, "Window Shade"),
        ],
    )
    fake.Lutron = lambda host, user, password: _FakeLutron(  # type: ignore[attr-defined]
        host, user, password, areas=[living, bedroom]
    )
    fake.Shade = _FakeShade  # type: ignore[attr-defined]
    fake.Output = _FakeOutput  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pylutron", fake)
    return types.SimpleNamespace(module=fake, living=living, bedroom=bedroom)


@pytest.fixture(autouse=True)
async def _reset_bridge() -> None:
    """Make sure each test starts with no shared bridge cached."""
    yield
    await reset_shared_bridge()


async def test_connect_loads_topology(
    fake_pylutron: types.SimpleNamespace,
) -> None:
    bridge = LutronBridge("10.0.0.1", "lutron", "integration")
    await bridge.connect()

    assert bridge.connected is True
    assert {o.name for o in bridge.lights} == {"Sofa Lamp", "Ceiling"}
    assert [s.name for s in bridge.shades] == ["Window Shade"]
    # Area mapping
    sofa = next(o for o in bridge.lights if o.name == "Sofa Lamp")
    assert bridge.area_of(sofa) == "Living Room"


async def test_id_lookup(fake_pylutron: types.SimpleNamespace) -> None:
    bridge = LutronBridge("10.0.0.1", "lutron", "integration")
    await bridge.connect()
    assert bridge.light_by_id("1") is not None
    assert bridge.light_by_id("2") is not None
    assert bridge.light_by_id("3") is None  # that's a shade
    assert bridge.shade_by_id("3") is not None


async def test_set_and_get_level(fake_pylutron: types.SimpleNamespace) -> None:
    bridge = LutronBridge("10.0.0.1", "lutron", "integration")
    await bridge.connect()
    sofa = bridge.light_by_id("1")
    assert sofa is not None
    await bridge.set_level(sofa, 75.0)
    assert sofa.set_level_calls == [75.0]
    level = await bridge.get_level(sofa)
    assert level == 75.0


async def test_stop_shade(fake_pylutron: types.SimpleNamespace) -> None:
    bridge = LutronBridge("10.0.0.1", "lutron", "integration")
    await bridge.connect()
    shade = bridge.shade_by_id("3")
    assert shade is not None
    await bridge.stop_shade(shade)
    assert shade.stop_calls == 1


async def test_shared_bridge_reuses_instance(
    fake_pylutron: types.SimpleNamespace,
) -> None:
    a = await shared_bridge("10.0.0.1", "lutron", "pw")
    b = await shared_bridge("10.0.0.1", "lutron", "pw")
    assert a is b


async def test_shared_bridge_rebuilds_on_credential_change(
    fake_pylutron: types.SimpleNamespace,
) -> None:
    a = await shared_bridge("10.0.0.1", "lutron", "pw1")
    b = await shared_bridge("10.0.0.1", "lutron", "pw2")
    assert a is not b
