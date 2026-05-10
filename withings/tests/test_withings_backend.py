"""Tests for the Withings OAuth pull backend.

Uses ``httpx.MockTransport`` so the OAuth + sync flows run end-to-end
against a deterministic stub HTTP server. The Withings sandbox is
fine for manual QA but isn't guaranteed deterministic enough for CI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gilbert_plugin_withings.withings_backend import WithingsBackend
from gilbert.interfaces.health import (
    HealthBackend,
    HealthBackendAuthError,
    MetricType,
    StorageAwareHealthBackend,
)
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    Query,
    StorageBackend,
)


# ── Fakes ────────────────────────────────────────────────────────────


class _DictStorage(StorageBackend):
    """In-memory storage that satisfies the StorageBackend ABC for the
    portions Withings actually touches (put/get/delete/query)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data[(collection, entity_id)] = dict(data)

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get((collection, entity_id))

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.pop((collection, entity_id), None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return (collection, entity_id) in self._data

    async def query(self, query: Query) -> list[dict[str, Any]]:
        return [v for (c, _), v in self._data.items() if c == query.collection]

    async def count(self, query: Query) -> int:
        return sum(1 for (c, _) in self._data if c == query.collection)

    async def delete_query(self, query: Query) -> int:
        before = len(self._data)
        for k in list(self._data.keys()):
            if k[0] == query.collection:
                del self._data[k]
        return before - len(self._data)

    async def list_collections(self) -> list[str]:
        return sorted({c for (c, _) in self._data})

    async def drop_collection(self, collection: str) -> None:
        for k in list(self._data.keys()):
            if k[0] == collection:
                del self._data[k]

    async def ensure_index(self, index: Any) -> None:
        return None

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        return None

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


# ── Helpers ──────────────────────────────────────────────────────────


def _ok_token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": 0,
            "body": {
                "access_token": "atk-1",
                "refresh_token": "rtk-1",
                "userid": "wuid-1",
                "expires_in": 10800,
            },
        },
    )


def _refreshed_token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": 0,
            "body": {
                "access_token": "atk-2",
                "refresh_token": "rtk-2",
                "userid": "wuid-1",
                "expires_in": 10800,
            },
        },
    )


def _make_backend(
    handler: httpx.MockTransport,
    storage: StorageBackend,
) -> WithingsBackend:
    backend = WithingsBackend()
    backend.set_storage(storage)
    backend.set_public_base_url("https://gilbert.test")
    # Replace the underlying client AFTER initialize to use the mock
    # transport so initialize doesn't issue any real network calls.
    backend._client = httpx.AsyncClient(transport=handler, timeout=5.0)
    backend._client_id = "cid"
    backend._client_secret = "csec"
    return backend


# ── Tests ────────────────────────────────────────────────────────────


def test_backend_registered() -> None:
    assert "withings" in HealthBackend.registered_backends()


def test_supports_pull() -> None:
    assert WithingsBackend().supports_pull is True
    assert WithingsBackend().supports_push is False


def test_satisfies_storage_aware_protocol() -> None:
    backend = WithingsBackend()
    assert isinstance(backend, StorageAwareHealthBackend)


async def test_begin_link_refuses_when_public_base_url_unset() -> None:
    storage = _DictStorage()
    backend = WithingsBackend()
    backend.set_storage(storage)
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    backend._client_id = "cid"
    backend._client_secret = "csec"
    backend.set_public_base_url("")
    result = await backend.begin_link("alice")
    assert result.status == "error"
    assert "public_base_url" in result.message


async def test_begin_link_persists_state_and_returns_url() -> None:
    storage = _DictStorage()
    handler = httpx.MockTransport(lambda r: httpx.Response(200))
    backend = _make_backend(handler, storage)

    result = await backend.begin_link("alice")
    assert result.status == "ok"
    assert result.open_url.startswith("https://account.withings.com/oauth2_user/authorize2")
    rows = await storage.query(Query(collection="health_oauth_state"))
    assert len(rows) == 1
    assert rows[0]["user_id"] == "alice"
    assert rows[0]["backend_name"] == "withings"


async def test_complete_link_exchanges_code_and_persists_tokens() -> None:
    storage = _DictStorage()

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v2/oauth2")
        return _ok_token_response()

    handler = httpx.MockTransport(_handler)
    backend = _make_backend(handler, storage)
    result = await backend.complete_link("alice", {"code": "abc"})
    assert result.status == "ok"
    link = await storage.get("health_links", "alice/withings")
    assert link is not None
    assert link["oauth_access_token"] == "atk-1"
    assert link["oauth_refresh_token"] == "rtk-1"
    assert link["oauth_user_id"] == "wuid-1"
    assert link["enabled"] is True


async def test_sync_returns_metrics_and_advances_cursor() -> None:
    storage = _DictStorage()
    # Pre-populate the link row with a valid token.
    await storage.put(
        "health_links",
        "alice/withings",
        {
            "_id": "alice/withings",
            "user_id": "alice",
            "backend_name": "withings",
            "oauth_access_token": "atk-1",
            "oauth_refresh_token": "rtk-1",
            "oauth_user_id": "wuid-1",
            "enabled": True,
        },
    )
    measure_response = httpx.Response(
        200,
        json={
            "status": 0,
            "body": {
                "measuregrps": [
                    {
                        "grpid": "g1",
                        "date": int(datetime(2026, 5, 9, 7, 0, tzinfo=UTC).timestamp()),
                        "measures": [
                            {
                                "value": 80500,
                                "type": 1,
                                "unit": -3,
                            }
                        ],
                    }
                ]
            },
        },
    )
    sleep_response = httpx.Response(
        200,
        json={
            "status": 0,
            "body": {
                "series": [
                    {
                        "id": "s1",
                        "startdate": int(datetime(2026, 5, 9, 0, 0, tzinfo=UTC).timestamp()),
                        "data": {
                            "totalsleepduration": 27000,
                            "deepsleepduration": 6000,
                        },
                    }
                ]
            },
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/measure" in request.url.path:
            return measure_response
        return sleep_response

    backend = _make_backend(httpx.MockTransport(_handler), storage)
    metrics = await backend.sync("alice")
    assert any(m.metric_type is MetricType.WEIGHT and m.value == 80.5 for m in metrics)
    assert any(m.metric_type is MetricType.SLEEP_DURATION for m in metrics)
    # Cursor advanced.
    link = await storage.get("health_links", "alice/withings")
    assert link is not None
    assert link.get("lastupdate", 0) > 0


async def test_sync_refreshes_on_401_and_retries() -> None:
    storage = _DictStorage()
    await storage.put(
        "health_links",
        "alice/withings",
        {
            "_id": "alice/withings",
            "user_id": "alice",
            "backend_name": "withings",
            "oauth_access_token": "atk-old",
            "oauth_refresh_token": "rtk-1",
            "oauth_user_id": "wuid-1",
            "enabled": True,
        },
    )
    state = {"meas_calls": 0, "token_calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/v2/oauth2" in request.url.path:
            state["token_calls"] += 1
            return _refreshed_token_response()
        if "/measure" in request.url.path:
            state["meas_calls"] += 1
            if state["meas_calls"] == 1:
                # Simulate "token expired" — Withings status code 401.
                return httpx.Response(
                    200, json={"status": 401, "error": "token expired"}
                )
            return httpx.Response(
                200, json={"status": 0, "body": {"measuregrps": []}}
            )
        return httpx.Response(
            200, json={"status": 0, "body": {"series": []}}
        )

    backend = _make_backend(httpx.MockTransport(_handler), storage)
    await backend.sync("alice")
    assert state["token_calls"] >= 1
    assert state["meas_calls"] >= 2
    link = await storage.get("health_links", "alice/withings")
    assert link is not None
    assert link["oauth_access_token"] == "atk-2"


async def test_sync_persistent_auth_failure_raises_HealthBackendAuthError() -> None:
    storage = _DictStorage()
    await storage.put(
        "health_links",
        "alice/withings",
        {
            "_id": "alice/withings",
            "user_id": "alice",
            "backend_name": "withings",
            "oauth_access_token": "atk-old",
            "oauth_refresh_token": "rtk-old",
            "oauth_user_id": "wuid-1",
            "enabled": True,
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/v2/oauth2" in request.url.path:
            # Refresh fails persistently.
            return httpx.Response(
                200, json={"status": 401, "error": "refresh failed"}
            )
        return httpx.Response(
            200, json={"status": 401, "error": "unauthorized"}
        )

    backend = _make_backend(httpx.MockTransport(_handler), storage)
    with pytest.raises(HealthBackendAuthError):
        await backend.sync("alice")


async def test_disconnect_revokes_upstream_then_returns() -> None:
    storage = _DictStorage()
    await storage.put(
        "health_links",
        "alice/withings",
        {
            "_id": "alice/withings",
            "user_id": "alice",
            "backend_name": "withings",
            "oauth_access_token": "atk-1",
            "oauth_user_id": "wuid-1",
            "enabled": True,
        },
    )
    revoke_calls = {"count": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if "/v2/oauth2" in request.url.path:
            data = request.read().decode()
            assert "action=revoke" in data
            revoke_calls["count"] += 1
            return httpx.Response(200, json={"status": 0})
        return httpx.Response(200)

    backend = _make_backend(httpx.MockTransport(_handler), storage)
    await backend.disconnect("alice")
    assert revoke_calls["count"] == 1


async def test_disconnect_swallows_upstream_failure() -> None:
    storage = _DictStorage()
    await storage.put(
        "health_links",
        "alice/withings",
        {
            "_id": "alice/withings",
            "user_id": "alice",
            "backend_name": "withings",
            "oauth_access_token": "atk-1",
            "oauth_user_id": "wuid-1",
            "enabled": True,
        },
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    backend = _make_backend(httpx.MockTransport(_handler), storage)
    # Local cleanup must succeed even when upstream is unreachable —
    # disconnect SHOULD NOT raise.
    await backend.disconnect("alice")


async def test_supported_metrics_includes_weight_sleep() -> None:
    backend = WithingsBackend()
    supported = backend.supported_metrics()
    assert MetricType.WEIGHT in supported
    assert MetricType.SLEEP_DURATION in supported

