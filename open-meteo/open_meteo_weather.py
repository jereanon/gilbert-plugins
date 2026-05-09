"""Open-Meteo weather backend — current, hourly, and daily forecasts.

No API key required. Free tier permits up to 600 requests/min,
5,000/hour, 10,000/day. Cache TTLs in ``WeatherService`` keep typical
home-assistant usage well under these limits.

**Commercial use requires a paid Open-Meteo plan / API key**; that's
documented in ``std-plugins/README.md`` and is out of scope for this
backend. The default ``user_agent`` carries a contact URL because
Open-Meteo's free-tier docs ask for a useful identifier.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.weather import (
    CurrentWeather,
    DailyForecast,
    GeoLocation,
    HourlyForecast,
    WeatherBackend,
    WeatherBackendCapabilities,
    WeatherUnavailableError,
    WeatherUnits,
)

# Use absolute-style import for plugin's own modules — registered
# under ``gilbert_plugin_open_meteo`` by the conftest in tests, and
# under the plugin's package name by the plugin loader at runtime.
try:
    from .weather_codes import code_to_condition
except ImportError:  # pragma: no cover — fallback for direct script execution
    from weather_codes import code_to_condition  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


class OpenMeteoWeather(WeatherBackend):
    """Open-Meteo HTTP backend."""

    backend_name = "open-meteo"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="timeout_seconds",
                type=ToolParameterType.INTEGER,
                description="HTTP request timeout in seconds.",
                default=15,
            ),
            ConfigParam(
                key="user_agent",
                type=ToolParameterType.STRING,
                description=(
                    "HTTP User-Agent for Open-Meteo requests. Be a good "
                    "citizen — identify your install."
                ),
                default="Gilbert/1.0 (https://github.com/briandilley/gilbert)",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Hit the Open-Meteo forecast endpoint for a known coordinate."
                ),
            ),
        ]

    def capabilities(self) -> WeatherBackendCapabilities:
        return WeatherBackendCapabilities(
            current=True, hourly=True, daily=True, alerts=False,
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._timeout: int = 15
        self._user_agent: str = "Gilbert/1.0 (https://github.com/briandilley/gilbert)"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._timeout = int(config.get("timeout_seconds", 15) or 15)
        self._user_agent = str(
            config.get("user_agent")
            or "Gilbert/1.0 (https://github.com/briandilley/gilbert)"
        )
        # Granular timeouts so a hung DNS / TLS handshake doesn't burn
        # the whole 15s budget on connect alone. Limits cap concurrent
        # connections — under cache-stampede or alert-poll churn this
        # prevents an unbounded socket fan-out.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": self._user_agent},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="Open-Meteo backend not initialized — save settings first.",
            )
        try:
            await self.current(
                GeoLocation(latitude=0.0, longitude=0.0, name="probe"),
                units=WeatherUnits.METRIC,
            )
        except WeatherUnavailableError as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message="Connected to Open-Meteo successfully.",
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _common_params(
        self,
        location: GeoLocation,
        units: WeatherUnits,
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "latitude": f"{location.latitude}",
            "longitude": f"{location.longitude}",
            "timezone": location.timezone or "auto",
        }
        if units is WeatherUnits.IMPERIAL:
            params["temperature_unit"] = "fahrenheit"
            params["wind_speed_unit"] = "mph"
            params["precipitation_unit"] = "inch"
        else:
            # Open-Meteo defaults are celsius / km/h / mm
            params["wind_speed_unit"] = "kmh"
        return params

    async def _fetch(
        self,
        url: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        if self._client is None:
            raise WeatherUnavailableError("OpenMeteoWeather not initialized")
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise WeatherUnavailableError(
                "Open-Meteo request timed out", retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise WeatherUnavailableError(
                f"Open-Meteo HTTP error: {exc}", retryable=True,
            ) from exc
        if response.status_code >= 500:
            raise WeatherUnavailableError(
                f"Open-Meteo returned {response.status_code}",
                provider_status=response.status_code,
                retryable=True,
            )
        if response.status_code >= 400:
            raise WeatherUnavailableError(
                f"Open-Meteo rejected the request ({response.status_code})",
                provider_status=response.status_code,
                retryable=False,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WeatherUnavailableError(
                "Open-Meteo returned invalid JSON", retryable=False,
            ) from exc

    # ── current() ────────────────────────────────────────────────────

    async def current(
        self,
        location: GeoLocation,
        *,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> CurrentWeather:
        params = self._common_params(location, units)
        params["current"] = ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "wind_speed_10m",
                "wind_gusts_10m",
                "wind_direction_10m",
                "pressure_msl",
                "precipitation",
                "cloud_cover",
                "weather_code",
            ]
        )
        data = await self._fetch(_FORECAST_URL, params)
        return _parse_current(data, location, units)

    async def forecast_hourly(
        self,
        location: GeoLocation,
        *,
        hours: int = 24,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[HourlyForecast]:
        if hours < 1:
            return []
        params = self._common_params(location, units)
        params["hourly"] = ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "precipitation",
                "precipitation_probability",
                "wind_speed_10m",
                "wind_gusts_10m",
                "wind_direction_10m",
                "cloud_cover",
                "weather_code",
            ]
        )
        # Open-Meteo limits forecast_hours; we also cap days appropriately.
        params["forecast_hours"] = str(min(max(hours, 1), 168))
        data = await self._fetch(_FORECAST_URL, params)
        return _parse_hourly(data, location, units, hours)

    async def forecast_daily(
        self,
        location: GeoLocation,
        *,
        days: int = 7,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[DailyForecast]:
        if days < 1:
            return []
        params = self._common_params(location, units)
        params["daily"] = ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "wind_gusts_10m_max",
                "sunrise",
                "sunset",
                "weather_code",
            ]
        )
        params["forecast_days"] = str(min(max(days, 1), 16))
        data = await self._fetch(_FORECAST_URL, params)
        return _parse_daily(data, location, units, days)

    async def geocode(self, query: str, *, count: int = 5) -> list[GeoLocation]:
        if not query.strip():
            return []
        params = {
            "name": query,
            "count": str(min(max(count, 1), 10)),
            "format": "json",
        }
        data = await self._fetch(_GEOCODING_URL, params)
        return _parse_geocoding(data)


# ── Parsers (module-level for testability) ────────────────────────────


def _parse_current(
    data: dict[str, Any],
    location: GeoLocation,
    units: WeatherUnits,
) -> CurrentWeather:
    block = data.get("current", {}) or {}
    if not block:
        raise WeatherUnavailableError(
            "Open-Meteo response missing 'current' block",
            retryable=False,
        )
    code = block.get("weather_code")
    condition = code_to_condition(code)
    observed_iso = str(block.get("time", "")) or datetime.now().isoformat()
    try:
        observed_at = datetime.fromisoformat(observed_iso)
    except ValueError:
        observed_at = datetime.now()
    # Resolve location timezone from response if present
    tz = str(data.get("timezone", location.timezone or "UTC"))
    resolved_loc = (
        GeoLocation(
            latitude=location.latitude,
            longitude=location.longitude,
            name=location.name,
            timezone=tz,
            country_code=location.country_code,
        )
        if tz and tz != location.timezone
        else location
    )
    return CurrentWeather(
        location=resolved_loc,
        observed_at=observed_at,
        temperature=_as_float(block.get("temperature_2m"), 0.0),
        feels_like=_as_optional_float(block.get("apparent_temperature")),
        humidity_pct=_as_optional_float(block.get("relative_humidity_2m")),
        wind_speed=_as_float(block.get("wind_speed_10m"), 0.0),
        wind_gust=_as_optional_float(block.get("wind_gusts_10m")),
        wind_direction_deg=_as_optional_float(block.get("wind_direction_10m")),
        pressure_hpa=_as_optional_float(block.get("pressure_msl")),
        precipitation_last_hour=_as_optional_float(block.get("precipitation")),
        cloud_cover_pct=_as_optional_float(block.get("cloud_cover")),
        condition=condition,
        raw_code=str(code) if code is not None else "",
        description="",
        units=units,
    )


def _parse_hourly(
    data: dict[str, Any],
    location: GeoLocation,
    units: WeatherUnits,
    cap: int,
) -> list[HourlyForecast]:
    block = data.get("hourly", {}) or {}
    times = block.get("time") or []
    if not isinstance(times, list) or not times:
        return []
    tz = str(data.get("timezone", location.timezone or "UTC"))
    resolved_loc = (
        GeoLocation(
            latitude=location.latitude,
            longitude=location.longitude,
            name=location.name,
            timezone=tz,
            country_code=location.country_code,
        )
        if tz and tz != location.timezone
        else location
    )
    temps = block.get("temperature_2m") or []
    feels = block.get("apparent_temperature") or []
    precip = block.get("precipitation") or []
    pop = block.get("precipitation_probability") or []
    wind = block.get("wind_speed_10m") or []
    gusts = block.get("wind_gusts_10m") or []
    wdir = block.get("wind_direction_10m") or []
    clouds = block.get("cloud_cover") or []
    codes = block.get("weather_code") or []
    out: list[HourlyForecast] = []
    n = min(len(times), cap)
    for i in range(n):
        try:
            valid_at = datetime.fromisoformat(str(times[i]))
        except ValueError:
            continue
        out.append(
            HourlyForecast(
                location=resolved_loc,
                valid_at=valid_at,
                temperature=_idx_float(temps, i, 0.0),
                feels_like=_idx_optional_float(feels, i),
                precipitation=_idx_float(precip, i, 0.0),
                precipitation_probability_pct=_idx_optional_float(pop, i),
                wind_speed=_idx_float(wind, i, 0.0),
                wind_gust=_idx_optional_float(gusts, i),
                wind_direction_deg=_idx_optional_float(wdir, i),
                cloud_cover_pct=_idx_optional_float(clouds, i),
                condition=code_to_condition(_idx_value(codes, i)),
                units=units,
            )
        )
    return out


def _parse_daily(
    data: dict[str, Any],
    location: GeoLocation,
    units: WeatherUnits,
    cap: int,
) -> list[DailyForecast]:
    block = data.get("daily", {}) or {}
    dates = block.get("time") or []
    if not isinstance(dates, list) or not dates:
        return []
    tz = str(data.get("timezone", location.timezone or "UTC"))
    resolved_loc = (
        GeoLocation(
            latitude=location.latitude,
            longitude=location.longitude,
            name=location.name,
            timezone=tz,
            country_code=location.country_code,
        )
        if tz and tz != location.timezone
        else location
    )
    highs = block.get("temperature_2m_max") or []
    lows = block.get("temperature_2m_min") or []
    precip = block.get("precipitation_sum") or []
    pop = block.get("precipitation_probability_max") or []
    wind = block.get("wind_speed_10m_max") or []
    gusts = block.get("wind_gusts_10m_max") or []
    sunrises = block.get("sunrise") or []
    sunsets = block.get("sunset") or []
    codes = block.get("weather_code") or []
    out: list[DailyForecast] = []
    n = min(len(dates), cap)
    for i in range(n):
        out.append(
            DailyForecast(
                location=resolved_loc,
                date=str(dates[i]),
                temperature_high=_idx_float(highs, i, 0.0),
                temperature_low=_idx_float(lows, i, 0.0),
                precipitation=_idx_float(precip, i, 0.0),
                precipitation_probability_pct=_idx_optional_float(pop, i),
                wind_speed_max=_idx_float(wind, i, 0.0),
                wind_gust_max=_idx_optional_float(gusts, i),
                sunrise=_idx_datetime(sunrises, i),
                sunset=_idx_datetime(sunsets, i),
                condition=code_to_condition(_idx_value(codes, i)),
                units=units,
            )
        )
    return out


def _parse_geocoding(data: dict[str, Any]) -> list[GeoLocation]:
    results = data.get("results") or []
    out: list[GeoLocation] = []
    if not isinstance(results, list):
        return out
    for r in results:
        try:
            lat = float(r.get("latitude"))
            lon = float(r.get("longitude"))
        except (TypeError, ValueError):
            continue
        name_parts: list[str] = []
        if r.get("name"):
            name_parts.append(str(r["name"]))
        if r.get("admin1"):
            name_parts.append(str(r["admin1"]))
        if r.get("country"):
            name_parts.append(str(r["country"]))
        out.append(
            GeoLocation(
                latitude=lat,
                longitude=lon,
                name=", ".join(name_parts),
                timezone=str(r.get("timezone", "UTC")),
                country_code=str(r.get("country_code", "")),
            )
        )
    return out


# ── Tiny float / list parsing helpers ─────────────────────────────────


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_optional_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _idx_value(seq: Any, i: int) -> Any:
    if isinstance(seq, list) and 0 <= i < len(seq):
        return seq[i]
    return None


def _idx_float(seq: Any, i: int, default: float) -> float:
    return _as_float(_idx_value(seq, i), default)


def _idx_optional_float(seq: Any, i: int) -> float | None:
    return _as_optional_float(_idx_value(seq, i))


def _idx_datetime(seq: Any, i: int) -> datetime | None:
    v = _idx_value(seq, i)
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None

