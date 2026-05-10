"""Tests for ``AppleHealthBackend``."""

from __future__ import annotations

import json

import pytest
from gilbert_plugin_apple_health.apple_health_backend import (
    AppleHealthBackend,
)

from gilbert.interfaces.health import HealthBackend, MetricType


async def _make_backend() -> AppleHealthBackend:
    backend = AppleHealthBackend()
    await backend.initialize({})
    return backend


def test_backend_registered() -> None:
    assert "apple-health" in HealthBackend.registered_backends()


async def test_parse_using_metric_type_value() -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "steps",
                    "value": 8431,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                }
            ]
        }
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert result[0].metric_type is MetricType.STEPS


async def test_parse_using_healthkit_identifier() -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierStepCount",
                    "value": 8431,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
                {
                    "type": "HKQuantityTypeIdentifierBodyMass",
                    "value": 80.5,
                    "unit": "kg",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
            ]
        }
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 2
    assert result[0].metric_type is MetricType.STEPS
    assert result[1].metric_type is MetricType.WEIGHT


async def test_unknown_healthkit_identifier_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "HKQuantityTypeIdentifierFancyNewMetric",
                    "value": 1.0,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
                {
                    "type": "HKQuantityTypeIdentifierStepCount",
                    "value": 8431,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
            ]
        }
    ).encode()
    with caplog.at_level("INFO"):
        result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert any(
        "unknown HealthKit identifier" in r.message for r in caplog.records
    )


async def test_extra_whitelist_keeps_device_and_source_app() -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "steps",
                    "value": 8431,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                    "extra": {
                        "device": "Apple Watch",
                        "source_app": "Health",
                        "x-forwarded-for": "1.2.3.4",
                        "user_agent": "Mozilla/5.0",
                    },
                }
            ]
        }
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert result[0].extra == {
        "device": "Apple Watch",
        "source_app": "Health",
    }
    # X-forwarded-for et al. dropped.
    assert "x-forwarded-for" not in result[0].extra
    assert "user_agent" not in result[0].extra


async def test_unknown_unit_drops_metric_with_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "steps",
                    "value": 1,
                    "unit": "stones",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                }
            ]
        }
    ).encode()
    with caplog.at_level("DEBUG"):
        result = await backend.parse_webhook("alice", body, {})
    assert result == []


async def test_malformed_json_raises() -> None:
    backend = await _make_backend()
    with pytest.raises(ValueError, match="malformed"):
        await backend.parse_webhook("alice", b"{not json", {})


async def test_supports_push_true() -> None:
    backend = await _make_backend()
    assert backend.supports_push is True
    assert backend.supports_pull is False


async def test_supported_metrics_includes_steps() -> None:
    backend = await _make_backend()
    assert MetricType.STEPS in backend.supported_metrics()
    assert MetricType.WEIGHT in backend.supported_metrics()
    assert MetricType.SLEEP_DURATION in backend.supported_metrics()

