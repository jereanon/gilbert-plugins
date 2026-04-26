"""NexiaThermostatBackend — ThermostatBackend backed by the ``nexia`` package.

The ``nexia`` library (https://pypi.org/project/nexia/) is the asyncio
client used by Home Assistant for American Standard / Trane / Nexia /
Asair thermostats. It speaks the Nexia cloud API and exposes
``NexiaHome`` (account-level), ``NexiaThermostat`` (gateway), and
``NexiaThermostatZone`` (per-zone state and control).

We model each *zone* as a Gilbert thermostat, since that's what users
actually adjust ("set the upstairs to 72"). Zone IDs are unique within
a thermostat, so we encode our ``thermostat_id`` as
``"<therm_id>:<zone_id>"`` to disambiguate across multiple gateways on
the same account.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.thermostat import (
    ThermostatBackend,
    ThermostatInfo,
)
from gilbert.interfaces.tools import ToolParameterType

if TYPE_CHECKING:
    from nexia.home import NexiaHome
    from nexia.thermostat import NexiaThermostat as NexiaTherm
    from nexia.zone import NexiaThermostatZone

logger = logging.getLogger(__name__)

# Set by ``plugin.py`` at setup time so the backend can persist its
# device UUID across restarts and avoid Nexia account-lockout from
# repeated device registrations.
_PLUGIN_DATA_DIR: Path | None = None


def set_plugin_data_dir(path: Path) -> None:
    """Called from ``plugin.py``'s ``setup()`` to wire the data dir.

    Module-level rather than per-backend because the backend is
    instantiated by the service registry without access to
    ``PluginContext``.
    """
    global _PLUGIN_DATA_DIR
    _PLUGIN_DATA_DIR = path


# Map our HVAC mode strings to nexia's uppercase constants.
_MODE_OUT = {
    "off": "OFF",
    "heat": "HEAT",
    "cool": "COOL",
    "auto": "AUTO",
}
_MODE_IN = {v: k for k, v in _MODE_OUT.items()}


def _split_id(thermostat_id: str) -> tuple[str, str]:
    """Split ``"<therm>:<zone>"`` ids back into the gateway+zone parts."""
    if ":" not in thermostat_id:
        raise KeyError(f"Malformed thermostat id: {thermostat_id!r}")
    therm_id, zone_id = thermostat_id.split(":", 1)
    return therm_id, zone_id


class NexiaThermostatBackend(ThermostatBackend):
    """Nexia cloud backend — supports heat, cool, auto, fan, humidity reads."""

    backend_name = "american-standard"
    supports_cooling = True
    supports_heating = True
    supports_fan_mode = True
    supports_humidity = True

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._brand: str = "nexia"
        self._session: aiohttp.ClientSession | None = None
        self._home: NexiaHome | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="username",
                type=ToolParameterType.STRING,
                description="Nexia / American Standard account email.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="password",
                type=ToolParameterType.STRING,
                description="Nexia / American Standard account password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="brand",
                type=ToolParameterType.STRING,
                description=(
                    "Account brand. Most users want 'nexia' (Nexia / Trane / "
                    "American Standard). Use 'asair' for Asair-branded accounts."
                ),
                default="nexia",
                restart_required=True,
                choices=("nexia", "asair"),
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Log in to the Nexia cloud and report the discovered "
                    "thermostat + zone counts."
                ),
                required_role="admin",
            ),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        self._username = str(config.get("username", "") or "")
        self._password = str(config.get("password", "") or "")
        self._brand = str(config.get("brand", "nexia") or "nexia")

        # Tear down any previous session/home so credential changes apply.
        await self._close_home()

        if not (self._username and self._password):
            logger.info("Nexia thermostat: not configured (missing credentials)")
            return

        try:
            await self._connect()
        except Exception:
            logger.exception("Nexia thermostat: failed to connect")

    async def close(self) -> None:
        await self._close_home()

    async def _close_home(self) -> None:
        async with self._lock:
            self._home = None
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    logger.exception("Error closing Nexia HTTP session")
                self._session = None

    async def _connect(self) -> None:
        from nexia.home import NexiaHome

        async with self._lock:
            if self._home is not None:
                return
            session = aiohttp.ClientSession()
            state_file = None
            if _PLUGIN_DATA_DIR is not None:
                _PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
                state_file = str(_PLUGIN_DATA_DIR / f"nexia-state-{self._username}.json")
            home = NexiaHome(
                session=session,
                username=self._username,
                password=self._password,
                brand=self._brand,
                state_file=state_file,
            )
            await home.login()
            await home.update()
            self._session = session
            self._home = home
            therm_ids = list(home.get_thermostat_ids())
            zone_total = 0
            for tid in therm_ids:
                try:
                    zone_total += len(home.get_thermostat_by_id(tid).get_zone_ids())
                except Exception:
                    logger.exception(
                        "Nexia: failed to enumerate zones for thermostat %s "
                        "during connect",
                        tid,
                    )
            logger.info(
                "Nexia connected — %d thermostat(s), %d zone(s) total: ids=%s",
                len(therm_ids),
                zone_total,
                therm_ids,
            )

    async def _home_or_connect(self) -> NexiaHome:
        if self._home is None:
            if not (self._username and self._password):
                raise RuntimeError("Nexia thermostat is not configured.")
            await self._connect()
        assert self._home is not None
        return self._home

    async def _refresh(self) -> NexiaHome:
        home = await self._home_or_connect()
        try:
            await home.update()
        except Exception:
            logger.exception("Nexia refresh failed (using cached state)")
        return home

    def _zone(self, home: NexiaHome, thermostat_id: str) -> tuple[NexiaTherm, NexiaThermostatZone]:
        therm_id, zone_id = _split_id(thermostat_id)
        therm = home.get_thermostat_by_id(_coerce_id(therm_id, home.get_thermostat_ids()))
        zone = therm.get_zone_by_id(_coerce_id(zone_id, therm.get_zone_ids()))
        return therm, zone

    def _info_from_zone(self, therm: NexiaTherm, zone: NexiaThermostatZone) -> ThermostatInfo:
        unit = _safe_call(therm, "get_unit", default="F") or "F"
        humidity: float | None = None
        if _safe_call(therm, "has_relative_humidity", default=False):
            raw = _safe_call(therm, "get_relative_humidity", default=None)
            if raw is not None:
                # Nexia reports humidity as 0..1 in some firmwares and 0..100
                # in others. Normalize: anything <= 1 is treated as a fraction.
                try:
                    fval = float(raw)
                    humidity = fval * 100.0 if fval <= 1.0 else fval
                except (TypeError, ValueError):
                    humidity = None
        fan_mode = _safe_call(therm, "get_fan_mode", default=None)
        fan_mode_str = str(fan_mode).lower() if fan_mode is not None else None
        mode_raw = _safe_call(zone, "get_current_mode", default="OFF") or "OFF"
        mode = _MODE_IN.get(str(mode_raw).upper(), str(mode_raw).lower())
        # Some offline / unsupported zones return None for temperature —
        # guard so a bad reading doesn't kill the whole list.
        temp = _optional_float(_safe_call(zone, "get_temperature", default=None))
        return ThermostatInfo(
            thermostat_id=f"{therm.thermostat_id}:{zone.zone_id}",
            name=_safe_call(zone, "get_name", default="(unknown)") or "(unknown)",
            area=_safe_call(therm, "get_name", default="") or "",
            supports_cooling=True,
            supports_heating=True,
            supports_fan_mode=True,
            has_humidity_sensor=humidity is not None,
            current_temperature=temp if temp is not None else 0.0,
            current_humidity=humidity,
            heat_setpoint=_optional_float(_safe_call(zone, "get_heating_setpoint", default=None)),
            cool_setpoint=_optional_float(_safe_call(zone, "get_cooling_setpoint", default=None)),
            mode=mode,
            fan_mode=fan_mode_str,
            temperature_unit=str(unit),
        )

    async def list_thermostats(self) -> list[ThermostatInfo]:
        try:
            home = await self._refresh()
        except Exception:
            return []
        out: list[ThermostatInfo] = []
        therm_ids = list(home.get_thermostat_ids())
        skipped = 0
        for therm_id in therm_ids:
            try:
                therm = home.get_thermostat_by_id(therm_id)
            except Exception:
                logger.exception("Nexia: failed to load thermostat %s", therm_id)
                skipped += 1
                continue
            try:
                zone_ids = list(therm.get_zone_ids())
            except Exception:
                logger.exception(
                    "Nexia: failed to enumerate zones on thermostat %s",
                    therm_id,
                )
                skipped += 1
                continue
            if not zone_ids:
                logger.warning(
                    "Nexia: thermostat %s reported zero zones — skipping",
                    therm_id,
                )
                continue
            for zone_id in zone_ids:
                try:
                    zone = therm.get_zone_by_id(zone_id)
                    out.append(self._info_from_zone(therm, zone))
                except Exception:
                    logger.exception(
                        "Nexia: failed to read zone %s on thermostat %s",
                        zone_id,
                        therm_id,
                    )
                    skipped += 1
        logger.info(
            "Nexia listed %d zone(s) across %d thermostat(s) (%d skipped)",
            len(out),
            len(therm_ids),
            skipped,
        )
        return out

    async def get_status(self, thermostat_id: str) -> ThermostatInfo:
        home = await self._refresh()
        therm, zone = self._zone(home, thermostat_id)
        return self._info_from_zone(therm, zone)

    async def set_setpoint(
        self,
        thermostat_id: str,
        *,
        heat: float | None = None,
        cool: float | None = None,
    ) -> None:
        home = await self._home_or_connect()
        _therm, zone = self._zone(home, thermostat_id)
        await zone.set_heat_cool_temp(
            heat_temperature=heat,
            cool_temperature=cool,
        )

    async def set_mode(self, thermostat_id: str, mode: str) -> None:
        home = await self._home_or_connect()
        _therm, zone = self._zone(home, thermostat_id)
        nexia_mode = _MODE_OUT.get(mode.lower())
        if nexia_mode is None:
            raise ValueError(f"Unsupported HVAC mode: {mode!r}")
        await zone.set_mode(nexia_mode)

    async def set_fan_mode(self, thermostat_id: str, fan_mode: str) -> None:
        home = await self._home_or_connect()
        therm, _zone = self._zone(home, thermostat_id)
        # Nexia's fan-mode label list is dynamic — match case-insensitively
        # against the device's reported labels so "auto"/"AUTO"/"Auto" all work.
        labels = _safe_call(therm, "get_fan_modes", default=None) or [
            "auto",
            "on",
            "circulate",
        ]
        wanted = fan_mode.lower()
        match = next(
            (label for label in labels if str(label).lower() == wanted),
            None,
        )
        if match is None:
            raise ValueError(
                f"Fan mode {fan_mode!r} not supported by this thermostat "
                f"(supports: {', '.join(str(label) for label in labels)})"
            )
        await therm.set_fan_mode(match)

    # --- BackendActionProvider ---

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key != "test_connection":
            return ConfigActionResult(
                status="error",
                message=f"Unknown action: {key}",
            )
        if not (self._username and self._password):
            return ConfigActionResult(
                status="error",
                message="Fill in username and password (and save) before testing.",
            )

        # Probe with a fresh, short-lived session so we exercise
        # credentials without disturbing the running connection.
        from nexia.home import NexiaHome

        session = aiohttp.ClientSession()
        try:
            home = NexiaHome(
                session=session,
                username=self._username,
                password=self._password,
                brand=self._brand,
            )
            await home.login()
            await home.update()
            therm_count = len(home.get_thermostat_ids())
            zone_count = sum(
                len(home.get_thermostat_by_id(tid).get_zone_ids())
                for tid in home.get_thermostat_ids()
            )
        except Exception as exc:
            logger.exception("Nexia test_connection failed")
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        finally:
            await session.close()

        return ConfigActionResult(
            status="ok",
            message=f"Connected — {therm_count} thermostat(s), {zone_count} zone(s).",
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _safe_call(obj: Any, method: str, *, default: Any) -> Any:
    """Call ``obj.method()`` if it exists, else return ``default``.

    Lets us defensively read attributes that may not exist on every
    nexia firmware/library version (e.g. ``get_relative_humidity``).
    """
    fn = getattr(obj, method, None)
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_id(raw: str, candidates: list[Any]) -> Any:
    """Match ``raw`` against the candidate ids, preserving original type.

    Nexia ids are sometimes ints and sometimes strings depending on
    library version. We stringify them at the boundary so our
    ``thermostat_id`` strings round-trip cleanly.
    """
    for candidate in candidates:
        if str(candidate) == raw:
            return candidate
    raise KeyError(f"Unknown id: {raw}")


# ── Module-level shared home (for symmetry with other plugins) ────────
#
# The backend instance owns its own NexiaHome, so this is a no-op in
# normal use — but ``plugin.py``'s teardown calls it to ensure any
# leaked sessions are released if the service registry didn't call
# ``close()`` for some reason.

async def reset_shared_home() -> None:  # pragma: no cover - defensive
    """Compatibility hook called from plugin teardown — no-op."""
    return
