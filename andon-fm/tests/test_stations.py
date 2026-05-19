"""Catalog + lookup tests."""

from __future__ import annotations

from gilbert_plugin_andon_fm.stations import BUNDLED_STATIONS, find_station


def test_bundled_catalog_has_four_unique_stations() -> None:
    assert len(BUNDLED_STATIONS) == 4
    ids = {s.id for s in BUNDLED_STATIONS}
    names = {s.name for s in BUNDLED_STATIONS}
    assert len(ids) == 4
    assert len(names) == 4
    for s in BUNDLED_STATIONS:
        assert s.stream_url.startswith("https://streaming.live365.com/")
        assert s.image_url.startswith("https://")


def test_find_station_by_id_name_substring_and_host() -> None:
    by_id = find_station("6b53fc38-ed57-4738-80d6-f9fddf981054")
    assert by_id is not None
    assert by_id.name == "Thinking Frequencies"

    assert find_station("openair") is not None
    assert find_station("OpenAIR").name == "OpenAIR"  # type: ignore[union-attr]
    assert find_station("backlink").name == "Backlink Broadcast"  # type: ignore[union-attr]

    # host-name fallback
    assert find_station("Claude").name == "Thinking Frequencies"  # type: ignore[union-attr]
    assert find_station("grok").name == "Grok and Roll"  # type: ignore[union-attr]


def test_find_station_returns_none_for_unknown() -> None:
    assert find_station("") is None
    assert find_station("   ") is None
    assert find_station("definitely-not-a-station") is None
