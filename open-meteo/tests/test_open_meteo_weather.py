"""Unit tests for the Open-Meteo backend.

External HTTP is mocked via ``httpx.MockTransport``. Fixtures use
the canonical Cleveland, OH coordinate so committing them doesn't
leak a developer's home location.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from gilbert_plugin_open_meteo.open_meteo_weather import (
    OpenMeteoWeather,
    _parse_current,
    _parse_daily,
    _parse_geocoding,
    _parse_hourly,
)

from gilbert.interfaces.weather import (
    GeoLocation,
    WeatherBackend,
    WeatherCondition,
    WeatherUnavailableError,
    WeatherUnits,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / name).read_text())


_CLEVELAND = GeoLocation(
    latitude=41.4993,
    longitude=-81.6944,
    name="Cleveland, OH, USA",
    timezone="America/New_York",
    country_code="US",
)


# ── Backend registration ─────────────────────────────────────────────


class TestRegistration:
    def test_backend_registers_under_open_meteo(self) -> None:
        registry = WeatherBackend.registered_backends()
        assert "open-meteo" in registry
        assert registry["open-meteo"] is OpenMeteoWeather

    def test_capabilities_no_alerts(self) -> None:
        caps = OpenMeteoWeather().capabilities()
        assert caps.current is True
        assert caps.hourly is True
        assert caps.daily is True
        assert caps.alerts is False


# ── Initialize / close ───────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_creates_client_with_granular_timeouts(self) -> None:
        backend = OpenMeteoWeather()
        await backend.initialize({"timeout_seconds": 20, "user_agent": "Test/1"})
        client = backend._client
        assert client is not None
        # httpx.Timeout exposes per-phase config — verify the constructor
        # received granular timeouts (not a bare 15s blanket).
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 10.0
        assert client.timeout.write == 5.0
        assert client.headers["User-Agent"] == "Test/1"
        await backend.close()
        assert backend._client is None


# ── Parsers (no HTTP) ────────────────────────────────────────────────


class TestParsers:
    def test_parse_current_full_shape(self) -> None:
        data = _load("forecast_response.json")
        cw = _parse_current(data, _CLEVELAND, WeatherUnits.METRIC)
        assert cw.temperature == pytest.approx(18.4)
        assert cw.feels_like == pytest.approx(17.1)
        assert cw.humidity_pct == pytest.approx(62.0)
        assert cw.wind_speed == pytest.approx(12.3)
        assert cw.wind_gust == pytest.approx(21.7)
        assert cw.wind_direction_deg == pytest.approx(240.0)
        assert cw.pressure_hpa == pytest.approx(1014.2)
        assert cw.precipitation_last_hour == pytest.approx(0.0)
        assert cw.cloud_cover_pct == pytest.approx(75.0)
        assert cw.condition is WeatherCondition.CLOUDY
        assert cw.units is WeatherUnits.METRIC
        assert cw.location.timezone == "America/New_York"

    def test_parse_current_missing_block_raises(self) -> None:
        with pytest.raises(WeatherUnavailableError):
            _parse_current({}, _CLEVELAND, WeatherUnits.METRIC)

    def test_parse_hourly_arrays_align(self) -> None:
        data = _load("forecast_response.json")
        items = _parse_hourly(data, _CLEVELAND, WeatherUnits.METRIC, cap=24)
        assert len(items) == 5
        assert items[0].valid_at == datetime.fromisoformat("2026-05-09T15:00")
        assert items[2].condition is WeatherCondition.RAIN
        assert items[2].precipitation == pytest.approx(0.5)
        assert items[2].precipitation_probability_pct == pytest.approx(65.0)

    def test_parse_hourly_caps_at_n(self) -> None:
        data = _load("forecast_response.json")
        items = _parse_hourly(data, _CLEVELAND, WeatherUnits.METRIC, cap=2)
        assert len(items) == 2

    def test_parse_daily_with_sunrise_sunset(self) -> None:
        data = _load("forecast_response.json")
        items = _parse_daily(data, _CLEVELAND, WeatherUnits.METRIC, cap=14)
        assert len(items) == 3
        first = items[0]
        assert first.date == "2026-05-09"
        assert first.temperature_high == pytest.approx(19.2)
        assert first.temperature_low == pytest.approx(11.4)
        assert first.precipitation == pytest.approx(1.6)
        assert first.condition is WeatherCondition.RAIN
        assert first.sunrise == datetime.fromisoformat("2026-05-09T06:14")
        assert first.sunset == datetime.fromisoformat("2026-05-09T20:34")
        # Last day — thunderstorm
        assert items[2].condition is WeatherCondition.THUNDERSTORM

    def test_parse_geocoding_two_candidates(self) -> None:
        data = _load("geocoding_response.json")
        items = _parse_geocoding(data)
        assert len(items) == 2
        ohio = items[0]
        assert ohio.latitude == pytest.approx(41.4993)
        assert ohio.longitude == pytest.approx(-81.6944)
        assert "Cleveland" in ohio.name
        assert "Ohio" in ohio.name
        assert ohio.timezone == "America/New_York"
        assert ohio.country_code == "US"

    def test_parse_geocoding_empty(self) -> None:
        assert _parse_geocoding({"results": []}) == []
        assert _parse_geocoding({}) == []


# ── HTTP via MockTransport ───────────────────────────────────────────


def _make_backend_with_mock(handler: Any) -> OpenMeteoWeather:
    """Construct a backend with a pre-baked transport for tests."""
    backend = OpenMeteoWeather()
    backend._timeout = 5
    backend._user_agent = "Test/1"
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "Test/1"},
    )
    return backend


class TestHTTP:
    @pytest.mark.asyncio
    async def test_current_round_trips_through_mock_transport(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_load("forecast_response.json"))

        backend = _make_backend_with_mock(handler)
        try:
            cw = await backend.current(_CLEVELAND, units=WeatherUnits.METRIC)
        finally:
            await backend.close()
        assert "latitude=41.4993" in captured["url"]
        assert "longitude=-81.6944" in captured["url"]
        assert "weather_code" in captured["url"]
        assert cw.temperature == pytest.approx(18.4)

    @pytest.mark.asyncio
    async def test_imperial_units_pass_through_to_query(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_load("forecast_response.json"))

        backend = _make_backend_with_mock(handler)
        try:
            await backend.current(_CLEVELAND, units=WeatherUnits.IMPERIAL)
        finally:
            await backend.close()
        assert "temperature_unit=fahrenheit" in captured["url"]
        assert "wind_speed_unit=mph" in captured["url"]
        assert "precipitation_unit=inch" in captured["url"]

    @pytest.mark.asyncio
    async def test_5xx_maps_to_retryable_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        backend = _make_backend_with_mock(handler)
        try:
            with pytest.raises(WeatherUnavailableError) as exc_info:
                await backend.current(_CLEVELAND)
        finally:
            await backend.close()
        assert exc_info.value.provider_status == 503
        assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_4xx_maps_to_non_retryable_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400)

        backend = _make_backend_with_mock(handler)
        try:
            with pytest.raises(WeatherUnavailableError) as exc_info:
                await backend.current(_CLEVELAND)
        finally:
            await backend.close()
        assert exc_info.value.provider_status == 400
        assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_timeout_maps_to_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        backend = _make_backend_with_mock(handler)
        try:
            with pytest.raises(WeatherUnavailableError) as exc_info:
                await backend.current(_CLEVELAND)
        finally:
            await backend.close()
        assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_invalid_json_maps_to_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        backend = _make_backend_with_mock(handler)
        try:
            with pytest.raises(WeatherUnavailableError):
                await backend.current(_CLEVELAND)
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_alerts_default_returns_empty(self) -> None:
        backend = OpenMeteoWeather()
        # No HTTP needed — the default impl returns [] from the ABC.
        assert await backend.alerts(_CLEVELAND) == []

    @pytest.mark.asyncio
    async def test_geocode_round_trips(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_load("geocoding_response.json"))

        backend = _make_backend_with_mock(handler)
        try:
            results = await backend.geocode("Cleveland", count=5)
        finally:
            await backend.close()
        assert len(results) == 2
        assert results[0].name.startswith("Cleveland")

    @pytest.mark.asyncio
    async def test_geocode_empty_query_short_circuits(self) -> None:
        backend = OpenMeteoWeather()
        await backend.initialize({})
        try:
            assert await backend.geocode("") == []
            assert await backend.geocode("   ") == []
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_test_connection_action_succeeds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_load("forecast_response.json"))

        backend = _make_backend_with_mock(handler)
        try:
            result = await backend.invoke_backend_action("test_connection", {})
        finally:
            await backend.close()
        assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_test_connection_action_fails_on_5xx(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        backend = _make_backend_with_mock(handler)
        try:
            result = await backend.invoke_backend_action("test_connection", {})
        finally:
            await backend.close()
        assert result.status == "error"
        assert "503" in result.message or "failed" in result.message.lower()

