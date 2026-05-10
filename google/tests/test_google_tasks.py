"""Tests for ``GoogleTasksBackend`` payload mapping and error mapping.

Mocks the ``googleapiclient`` ``service`` object — we test the backend
itself, not the Google client.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from gilbert_plugin_google.google_tasks import GoogleTasksBackend

from gilbert.interfaces.tasks import (
    Task,
    TaskBackendAuthError,
    TaskBackendError,
    TaskBackendNotFoundError,
    TaskBackendRateLimitError,
    TaskBackendTransientError,
    TaskStatus,
)


def _make_backend(*, tasklist_id: str = "list_a") -> tuple[GoogleTasksBackend, MagicMock]:
    backend = GoogleTasksBackend()
    backend._tasklist_id = tasklist_id
    fake_service = MagicMock()
    backend._service = fake_service
    return backend, fake_service


def _http_error(
    status: int, reason: str = "", *, headers: dict[str, str] | None = None
) -> Exception:
    """Build a googleapiclient.errors.HttpError-like instance."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    resp.headers = dict(headers or {})
    content_obj = {"error": {"errors": [{"reason": reason}]}}
    return HttpError(resp, json.dumps(content_obj).encode("utf-8"))


# ── Payload → Task ──────────────────────────────────────────────────


class TestTaskMapping:
    def test_open_task_with_due_date(self) -> None:
        data: dict[str, Any] = {
            "id": "abc123",
            "etag": "etag_001",
            "title": "Buy milk",
            "notes": "1%, almond",
            "due": "2026-05-10T00:00:00.000Z",
            "status": "needsAction",
            "updated": "2026-05-09T17:00:00.000Z",
        }
        t = GoogleTasksBackend._to_task(data, project="My Tasks")
        assert t.source_id == "abc123"
        assert t.title == "Buy milk"
        assert t.notes == "1%, almond"
        assert t.due_at == "2026-05-10T00:00:00Z"
        assert t.due_at_tz == "UTC"
        assert t.status == TaskStatus.OPEN
        assert t.etag == "etag_001"
        assert t.project == "My Tasks"
        assert t.updated_at == "2026-05-09T17:00:00Z"

    def test_completed_task(self) -> None:
        data: dict[str, Any] = {
            "id": "done1",
            "title": "Done",
            "status": "completed",
            "completed": "2026-05-09T18:00:00Z",
        }
        t = GoogleTasksBackend._to_task(data)
        assert t.status == TaskStatus.DONE
        assert t.completed_at == "2026-05-09T18:00:00Z"

    def test_no_due_date_leaves_tz_empty(self) -> None:
        data: dict[str, Any] = {"id": "x", "title": "no-due"}
        t = GoogleTasksBackend._to_task(data)
        assert t.due_at == ""
        assert t.due_at_tz == ""


# ── Task → Google body ──────────────────────────────────────────────


class TestRequestSerialization:
    def test_open_task_body(self) -> None:
        task = Task(
            id="x",
            list_id="local",
            title="Hello",
            notes="some notes",
            due_at="2026-05-10T00:00:00Z",
        )
        body = GoogleTasksBackend._task_to_body(task)
        assert body == {
            "title": "Hello",
            "notes": "some notes",
            "due": "2026-05-10T00:00:00Z",
        }

    def test_done_task_body_includes_status(self) -> None:
        task = Task(id="x", title="X", status=TaskStatus.DONE)
        body = GoogleTasksBackend._task_to_body(task)
        assert body == {"title": "X", "status": "completed"}

    def test_patch_to_body_only_includes_translated_fields(self) -> None:
        body = GoogleTasksBackend._patch_to_body(
            {
                "title": "new",
                "due_at": "2026-05-10T00:00:00Z",
                "priority": 3,  # Google has no native priority — dropped
                "tags": ["a"],  # Google has no native tags — dropped
            }
        )
        assert body == {"title": "new", "due": "2026-05-10T00:00:00Z"}


# ── Error mapping ───────────────────────────────────────────────────


class TestErrorMapping:
    def test_401_maps_to_auth_error(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(_http_error(401, "authError"))
        assert isinstance(mapped, TaskBackendAuthError)

    def test_403_with_invalid_grant_maps_to_auth_error(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(
            _http_error(403, "invalid_grant")
        )
        assert isinstance(mapped, TaskBackendAuthError)

    def test_404_maps_to_not_found(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(_http_error(404, "notFound"))
        assert isinstance(mapped, TaskBackendNotFoundError)

    def test_429_maps_to_rate_limit(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(_http_error(429, ""))
        assert isinstance(mapped, TaskBackendRateLimitError)

    def test_429_parses_retry_after_header(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(
            _http_error(
                429, "rateLimitExceeded", headers={"Retry-After": "30"}
            )
        )
        assert isinstance(mapped, TaskBackendRateLimitError)
        assert mapped.retry_after_sec == 30.0

    def test_500_maps_to_transient(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(_http_error(500, ""))
        assert isinstance(mapped, TaskBackendTransientError)

    def test_timeout_maps_to_transient(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(TimeoutError("slow"))
        assert isinstance(mapped, TaskBackendTransientError)

    def test_connection_error_maps_to_transient(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(ConnectionError("nope"))
        assert isinstance(mapped, TaskBackendTransientError)

    def test_unknown_error_falls_through_to_base(self) -> None:
        mapped = GoogleTasksBackend._map_http_error(RuntimeError("???"))
        assert isinstance(mapped, TaskBackendError)
        assert not isinstance(mapped, TaskBackendAuthError)
        assert not isinstance(mapped, TaskBackendTransientError)


# ── Live API surface (mocked) ───────────────────────────────────────


@pytest.mark.asyncio
class TestLiveAPISurface:
    async def test_list_tasks_empty(self) -> None:
        backend, fake = _make_backend()
        fake.tasklists.return_value.get.return_value.execute.return_value = {
            "title": "My Tasks"
        }
        fake.tasks.return_value.list.return_value.execute.return_value = {
            "items": [],
        }
        results = await backend.list_tasks()
        assert results == []

    async def test_list_tasks_returns_mapped(self) -> None:
        backend, fake = _make_backend()
        fake.tasklists.return_value.get.return_value.execute.return_value = {
            "title": "Work"
        }
        fake.tasks.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "src-1",
                    "title": "A",
                    "status": "needsAction",
                    "updated": "2026-05-09T10:00:00Z",
                },
                {
                    "id": "src-2",
                    "title": "B",
                    "status": "completed",
                    "updated": "2026-05-09T11:00:00Z",
                    "completed": "2026-05-09T11:00:00Z",
                },
            ],
        }
        results = await backend.list_tasks(include_completed=True)
        assert {t.source_id for t in results} == {"src-1", "src-2"}
        assert all(t.project == "Work" for t in results)

    async def test_add_task_returns_source_id(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.insert.return_value.execute.return_value = {
            "id": "upstream-id-xyz",
            "title": "Hello",
            "status": "needsAction",
        }
        result = await backend.add_task(Task(title="Hello"))
        assert result.source_id == "upstream-id-xyz"
        # Verify body sent.
        body = fake.tasks.return_value.insert.call_args.kwargs["body"]
        assert body == {"title": "Hello"}

    async def test_update_task_patches_only_provided_fields(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.patch.return_value.execute.return_value = {
            "id": "x",
            "title": "renamed",
            "status": "needsAction",
        }
        await backend.update_task("x", {"title": "renamed"})
        body = fake.tasks.return_value.patch.call_args.kwargs["body"]
        assert body == {"title": "renamed"}
        # Verify task id passed.
        assert fake.tasks.return_value.patch.call_args.kwargs["task"] == "x"

    async def test_complete_task_sends_completed_status(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.patch.return_value.execute.return_value = {
            "id": "x",
            "title": "y",
            "status": "completed",
        }
        await backend.complete_task("x")
        body = fake.tasks.return_value.patch.call_args.kwargs["body"]
        assert body == {"status": "completed"}

    async def test_complete_task_swallows_404(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.patch.return_value.execute.side_effect = (
            _http_error(404, "notFound")
        )
        # MUST NOT raise — naturally idempotent.
        await backend.complete_task("gone")

    async def test_delete_task_calls_tasks_delete(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.delete.return_value.execute.return_value = {}
        await backend.delete_task("x")
        assert fake.tasks.return_value.delete.call_args.kwargs["task"] == "x"

    async def test_delete_task_swallows_404(self) -> None:
        backend, fake = _make_backend()
        fake.tasks.return_value.delete.return_value.execute.side_effect = (
            _http_error(404, "notFound")
        )
        # MUST NOT raise — naturally idempotent.
        await backend.delete_task("gone")


# ── Initialization ──────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_missing_delegated_user_raises(self) -> None:
        b = GoogleTasksBackend()
        with pytest.raises(TaskBackendAuthError):
            await b.initialize(
                {"service_account_json": "{}"}
            )

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        b = GoogleTasksBackend()
        with pytest.raises(TaskBackendAuthError):
            await b.initialize(
                {
                    "service_account_json": "not-json",
                    "delegated_user": "u@example.com",
                }
            )


# ── Backend registry ────────────────────────────────────────────────


class TestBackendRegistry:
    def test_backend_name(self) -> None:
        assert GoogleTasksBackend.backend_name == "google_tasks"

    def test_registered(self) -> None:
        from gilbert.interfaces.tasks import TaskBackend

        assert "google_tasks" in TaskBackend.registered_backends()

