"""Tests for ``HKWebhookBackend``."""

from __future__ import annotations

import json

import pytest
from gilbert_plugin_hk_webhook.hk_webhook_backend import HKWebhookBackend

from gilbert.interfaces.health import HealthBackend, MetricType


async def _make_backend() -> HKWebhookBackend:
    backend = HKWebhookBackend()
    await backend.initialize({})
    return backend


def test_backend_registered() -> None:
    """The plugin's backend registers itself on
    ``HealthBackend._registry`` via ``__init_subclass__``."""
    assert "hk-webhook" in HealthBackend.registered_backends()


async def test_parse_metrics_array_form() -> None:
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
    assert result[0].value == 8431.0


async def test_parse_top_level_array() -> None:
    backend = await _make_backend()
    body = json.dumps(
        [
            {
                "type": "weight",
                "value": 80.5,
                "unit": "kg",
                "recorded_at": "2026-05-09T07:00:00+00:00",
            }
        ]
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert result[0].metric_type is MetricType.WEIGHT


async def test_parse_single_object() -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "type": "weight",
            "value": 80.5,
            "unit": "kg",
            "recorded_at": "2026-05-09T07:00:00+00:00",
        }
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1


async def test_unknown_type_dropped_with_log(caplog: pytest.LogCaptureFixture) -> None:
    backend = await _make_backend()
    body = json.dumps(
        {
            "metrics": [
                {
                    "type": "alien_radiation",
                    "value": 1.0,
                    "unit": "ms",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
                {
                    "type": "steps",
                    "value": 100,
                    "unit": "count",
                    "recorded_at": "2026-05-09T07:00:00+00:00",
                },
            ]
        }
    ).encode()
    with caplog.at_level("INFO"):
        result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert result[0].metric_type is MetricType.STEPS
    assert any("unknown metric type" in r.message.lower() for r in caplog.records)


async def test_extra_field_silently_stripped() -> None:
    """hk-webhook declares NO ``extra`` whitelist (§4.5) — every key
    in the payload's ``extra`` dict is dropped."""
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
                        "x-forwarded-for": "1.2.3.4",
                        "device": "iPhone",
                    },
                }
            ]
        }
    ).encode()
    result = await backend.parse_webhook("alice", body, {})
    assert len(result) == 1
    assert result[0].extra == {}


async def test_malformed_json_raises() -> None:
    backend = await _make_backend()
    with pytest.raises(ValueError, match="malformed"):
        await backend.parse_webhook("alice", b"{not json", {})


async def test_non_object_payload_raises() -> None:
    backend = await _make_backend()
    with pytest.raises(ValueError, match="array or object"):
        await backend.parse_webhook("alice", json.dumps("nope").encode(), {})


async def test_empty_body_returns_empty() -> None:
    backend = await _make_backend()
    result = await backend.parse_webhook("alice", b"", {})
    assert result == []


async def test_supports_push_true_pull_false() -> None:
    backend = await _make_backend()
    assert backend.supports_push is True
    assert backend.supports_pull is False


async def test_supported_metrics_includes_all() -> None:
    backend = await _make_backend()
    supported = backend.supported_metrics()
    # Generic backend claims every known type.
    assert MetricType.STEPS in supported
    assert MetricType.WEIGHT in supported

