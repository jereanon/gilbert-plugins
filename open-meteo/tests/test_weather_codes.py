"""Unit tests for the WMO 4677 weather-code mapping."""

from __future__ import annotations

import pytest
from gilbert_plugin_open_meteo.weather_codes import WMO_CODE_MAP, code_to_condition

from gilbert.interfaces.weather import WeatherCondition


class TestCodeToCondition:
    def test_clear_sky(self) -> None:
        assert code_to_condition(0) is WeatherCondition.CLEAR

    def test_partly_cloudy_codes(self) -> None:
        assert code_to_condition(1) is WeatherCondition.PARTLY_CLOUDY
        assert code_to_condition(2) is WeatherCondition.PARTLY_CLOUDY

    def test_overcast(self) -> None:
        assert code_to_condition(3) is WeatherCondition.CLOUDY

    def test_smoke_haze_legacy(self) -> None:
        assert code_to_condition(4) is WeatherCondition.SMOKE
        assert code_to_condition(5) is WeatherCondition.HAZE

    def test_fog(self) -> None:
        assert code_to_condition(45) is WeatherCondition.FOG
        assert code_to_condition(48) is WeatherCondition.FOG

    def test_drizzle_variants(self) -> None:
        assert code_to_condition(51) is WeatherCondition.DRIZZLE
        assert code_to_condition(53) is WeatherCondition.DRIZZLE
        assert code_to_condition(55) is WeatherCondition.DRIZZLE

    def test_freezing_drizzle(self) -> None:
        assert code_to_condition(56) is WeatherCondition.FREEZING_DRIZZLE
        assert code_to_condition(57) is WeatherCondition.FREEZING_DRIZZLE

    def test_rain_variants(self) -> None:
        assert code_to_condition(61) is WeatherCondition.RAIN
        assert code_to_condition(63) is WeatherCondition.RAIN
        assert code_to_condition(65) is WeatherCondition.HEAVY_RAIN

    def test_freezing_rain(self) -> None:
        assert code_to_condition(66) is WeatherCondition.FREEZING_RAIN
        assert code_to_condition(67) is WeatherCondition.FREEZING_RAIN

    def test_snow_variants(self) -> None:
        assert code_to_condition(71) is WeatherCondition.SNOW
        assert code_to_condition(73) is WeatherCondition.SNOW
        assert code_to_condition(75) is WeatherCondition.HEAVY_SNOW
        assert code_to_condition(77) is WeatherCondition.SNOW   # snow grains

    def test_rain_showers(self) -> None:
        assert code_to_condition(80) is WeatherCondition.RAIN
        assert code_to_condition(81) is WeatherCondition.RAIN
        assert code_to_condition(82) is WeatherCondition.HEAVY_RAIN

    def test_snow_showers(self) -> None:
        assert code_to_condition(85) is WeatherCondition.SNOW
        assert code_to_condition(86) is WeatherCondition.HEAVY_SNOW

    def test_thunderstorm(self) -> None:
        assert code_to_condition(95) is WeatherCondition.THUNDERSTORM

    def test_thunderstorm_with_hail(self) -> None:
        assert code_to_condition(96) is WeatherCondition.THUNDERSTORM_HAIL
        assert code_to_condition(99) is WeatherCondition.THUNDERSTORM_HAIL

    def test_unknown_codes_are_unknown_not_raise(self) -> None:
        # Codes not in the documented set must map to UNKNOWN (graceful)
        for code in (10, 20, 30, 100, 999):
            assert code_to_condition(code) is WeatherCondition.UNKNOWN

    def test_none_and_garbage_are_unknown(self) -> None:
        assert code_to_condition(None) is WeatherCondition.UNKNOWN
        assert code_to_condition("not a number") is WeatherCondition.UNKNOWN

    def test_string_int_works(self) -> None:
        assert code_to_condition("0") is WeatherCondition.CLEAR
        assert code_to_condition("95") is WeatherCondition.THUNDERSTORM

    @pytest.mark.parametrize("code", list(range(0, 100)))
    def test_all_codes_0_99_resolve_without_raising(self, code: int) -> None:
        # Graceful contract: every code in 0..99 must return some
        # WeatherCondition value (UNKNOWN is fine for undocumented codes).
        assert isinstance(code_to_condition(code), WeatherCondition)

    def test_all_documented_codes_are_non_unknown(self) -> None:
        # Every code we've explicitly mapped must be non-UNKNOWN.
        for code, cond in WMO_CODE_MAP.items():
            assert cond is not WeatherCondition.UNKNOWN, (
                f"WMO code {code} mapped to UNKNOWN — that's almost"
                f" certainly a bug in the mapping table."
            )

