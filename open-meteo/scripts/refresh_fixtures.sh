#!/usr/bin/env bash
# Re-record open-meteo fixture JSON from the live API. Run when the
# upstream contract shifts. CI never runs live API tests; this is a
# developer-only convenience.
#
# Uses Cleveland, OH coordinates (41.4993, -81.6944) deliberately so
# committed fixtures don't dox a developer's home location.
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p tests/fixtures

LAT="41.4993"
LON="-81.6944"
TZ="America/New_York"
UA="Gilbert/1.0 (https://github.com/briandilley/gilbert)"

curl -s -A "$UA" \
  "https://api.open-meteo.com/v1/forecast?latitude=$LAT&longitude=$LON&timezone=$TZ&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl,precipitation,cloud_cover,weather_code&hourly=temperature_2m,apparent_temperature,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,wind_direction_10m,cloud_cover,weather_code&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,weather_code&forecast_hours=5&forecast_days=3" \
  | python -m json.tool > tests/fixtures/forecast_response.json

curl -s -A "$UA" \
  "https://geocoding-api.open-meteo.com/v1/search?name=Cleveland&count=2&format=json" \
  | python -m json.tool > tests/fixtures/geocoding_response.json

echo "Refreshed fixtures from live Open-Meteo API."
