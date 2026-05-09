"""WMO 4677 weather-code → ``WeatherCondition`` mapping.

Open-Meteo's ``weather_code`` follows WMO 4677. Codes outside the
documented set map to ``WeatherCondition.UNKNOWN`` rather than
raising — graceful unknown is the contract.
"""

from __future__ import annotations

from gilbert.interfaces.weather import WeatherCondition

# WMO code -> WeatherCondition. Codes not present here map to UNKNOWN.
WMO_CODE_MAP: dict[int, WeatherCondition] = {
    0: WeatherCondition.CLEAR,
    1: WeatherCondition.PARTLY_CLOUDY,
    2: WeatherCondition.PARTLY_CLOUDY,
    3: WeatherCondition.CLOUDY,
    4: WeatherCondition.SMOKE,           # legacy WMO smoke
    5: WeatherCondition.HAZE,            # legacy WMO haze
    45: WeatherCondition.FOG,
    48: WeatherCondition.FOG,            # depositing rime fog
    51: WeatherCondition.DRIZZLE,
    53: WeatherCondition.DRIZZLE,
    55: WeatherCondition.DRIZZLE,
    56: WeatherCondition.FREEZING_DRIZZLE,
    57: WeatherCondition.FREEZING_DRIZZLE,
    61: WeatherCondition.RAIN,
    63: WeatherCondition.RAIN,
    65: WeatherCondition.HEAVY_RAIN,
    66: WeatherCondition.FREEZING_RAIN,
    67: WeatherCondition.FREEZING_RAIN,
    71: WeatherCondition.SNOW,
    73: WeatherCondition.SNOW,
    75: WeatherCondition.HEAVY_SNOW,
    77: WeatherCondition.SNOW,           # snow grains
    80: WeatherCondition.RAIN,           # rain showers slight
    81: WeatherCondition.RAIN,           # rain showers moderate
    82: WeatherCondition.HEAVY_RAIN,     # rain showers violent
    85: WeatherCondition.SNOW,           # snow showers slight
    86: WeatherCondition.HEAVY_SNOW,     # snow showers heavy
    95: WeatherCondition.THUNDERSTORM,
    96: WeatherCondition.THUNDERSTORM_HAIL,
    99: WeatherCondition.THUNDERSTORM_HAIL,
}


def code_to_condition(code: int | str | None) -> WeatherCondition:
    """Return the ``WeatherCondition`` for a WMO code (graceful UNKNOWN)."""
    if code is None:
        return WeatherCondition.UNKNOWN
    try:
        c = int(code)
    except (TypeError, ValueError):
        return WeatherCondition.UNKNOWN
    return WMO_CODE_MAP.get(c, WeatherCondition.UNKNOWN)

