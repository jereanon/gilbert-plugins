"""Tests for ``GoogleCalendarBackend`` payload mapping and error mapping.

Mocks the ``googleapiclient`` ``service`` object — we test the
backend itself, not the Google client.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from gilbert_plugin_google.google_calendar import GoogleCalendarBackend

from gilbert.interfaces.calendar import (
    AttendeeResponseStatus,
    CalendarAttendee,
    CalendarBackendAuthError,
    CalendarBackendConflictError,
    CalendarBackendError,
    CalendarBackendNotFoundError,
    CalendarBackendRateLimitError,
    CalendarBackendTransientError,
    EventCreateRequest,
    EventStatus,
    EventVisibility,
)


def _make_backend() -> tuple[GoogleCalendarBackend, MagicMock]:
    backend = GoogleCalendarBackend()
    backend._email_address = "alice@example.com"
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


# ── Payload → CalendarEvent ──────────────────────────────────────────


class TestEventMapping:
    def test_timed_event_with_attendees(self) -> None:
        data: dict[str, Any] = {
            "id": "evt_001",
            "etag": "etag_001",
            "summary": "Standup",
            "description": "Daily sync",
            "location": "Room 1",
            "htmlLink": "https://calendar.google.com/event?eid=001",
            "start": {
                "dateTime": "2026-05-09T09:00:00-04:00",
                "timeZone": "America/New_York",
            },
            "end": {
                "dateTime": "2026-05-09T09:30:00-04:00",
                "timeZone": "America/New_York",
            },
            "status": "confirmed",
            "visibility": "private",
            "transparency": "opaque",
            "organizer": {"email": "alice@example.com"},
            "attendees": [
                {
                    "email": "alice@example.com",
                    "displayName": "Alice",
                    "responseStatus": "accepted",
                    "self": True,
                    "organizer": True,
                },
                {
                    "email": "bob@example.com",
                    "responseStatus": "declined",
                },
            ],
        }
        evt = GoogleCalendarBackend._to_calendar_event(
            data, account_id="cal_a", calendar_id="primary"
        )
        assert evt.event_id == "evt_001"
        assert evt.title == "Standup"
        assert evt.start.tzinfo is not None
        assert evt.start.utcoffset() == timedelta(hours=-4)
        assert evt.etag == "etag_001"
        assert evt.html_link == "https://calendar.google.com/event?eid=001"
        assert evt.visibility == EventVisibility.PRIVATE
        assert evt.status == EventStatus.CONFIRMED
        assert evt.transparency == "opaque"
        assert len(evt.attendees) == 2
        assert evt.attendees[0].response_status == AttendeeResponseStatus.ACCEPTED
        assert evt.attendees[0].is_self is True
        assert evt.attendees[1].response_status == AttendeeResponseStatus.DECLINED

    def test_all_day_event(self) -> None:
        data: dict[str, Any] = {
            "id": "evt_ad",
            "summary": "Vacation",
            "start": {"date": "2026-05-09"},
            "end": {"date": "2026-05-12"},
            "status": "confirmed",
        }
        evt = GoogleCalendarBackend._to_calendar_event(
            data, account_id="cal_a", calendar_id="primary"
        )
        assert evt.all_day is True
        assert evt.start.date() == datetime(2026, 5, 9).date()
        assert evt.end.date() == datetime(2026, 5, 12).date()
        assert evt.start.tzinfo is not None

    def test_recurring_instance_carries_series_id(self) -> None:
        data: dict[str, Any] = {
            "id": "evt_rec_2026-05-09",
            "summary": "Weekly",
            "start": {"dateTime": "2026-05-09T09:00:00+00:00"},
            "end": {"dateTime": "2026-05-09T10:00:00+00:00"},
            "status": "confirmed",
            "recurringEventId": "evt_rec_series",
        }
        evt = GoogleCalendarBackend._to_calendar_event(
            data, account_id="cal_a", calendar_id="primary"
        )
        assert evt.recurring_event_id == "evt_rec_series"

    def test_zero_length_event_extends_to_one_hour(self) -> None:
        data: dict[str, Any] = {
            "id": "evt_z",
            "summary": "Zero",
            "start": {"dateTime": "2026-05-09T09:00:00+00:00"},
            "end": {"dateTime": "2026-05-09T09:00:00+00:00"},
            "status": "confirmed",
        }
        evt = GoogleCalendarBackend._to_calendar_event(
            data, account_id="cal_a", calendar_id="primary"
        )
        assert (evt.end - evt.start) == timedelta(hours=1)

    def test_unknown_response_status_defaults_to_needs_action(self) -> None:
        data: dict[str, Any] = {
            "id": "evt_u",
            "summary": "X",
            "start": {"dateTime": "2026-05-09T09:00:00+00:00"},
            "end": {"dateTime": "2026-05-09T09:30:00+00:00"},
            "status": "confirmed",
            "attendees": [
                {"email": "x@y.com", "responseStatus": "garbage"},
            ],
        }
        evt = GoogleCalendarBackend._to_calendar_event(data, account_id="", calendar_id="primary")
        assert evt.attendees[0].response_status == AttendeeResponseStatus.NEEDS_ACTION


# ── EventCreateRequest → Google body ─────────────────────────────────


class TestRequestSerialization:
    def test_timed_event_body(self) -> None:
        req = EventCreateRequest(
            title="Coffee",
            start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
            description="Catch up",
            location="Cafe",
            attendees=[CalendarAttendee(email="bob@example.com", name="Bob")],
            visibility=EventVisibility.PRIVATE,
        )
        body = GoogleCalendarBackend._request_to_body(req)
        assert body["summary"] == "Coffee"
        assert body["description"] == "Catch up"
        assert body["location"] == "Cafe"
        assert body["visibility"] == "private"
        assert body["start"]["dateTime"] == "2026-06-01T09:00:00+00:00"
        assert body["end"]["dateTime"] == "2026-06-01T09:30:00+00:00"
        assert body["attendees"] == [{"email": "bob@example.com", "displayName": "Bob"}]

    def test_all_day_event_uses_date_keys(self) -> None:
        req = EventCreateRequest(
            title="Vacation",
            start=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 7, 5, 0, 0, tzinfo=UTC),
            all_day=True,
        )
        body = GoogleCalendarBackend._request_to_body(req)
        assert body["start"] == {"date": "2026-07-01"}
        assert body["end"] == {"date": "2026-07-05"}


# ── Error mapping ─────────────────────────────────────────────────────


class TestErrorMapping:
    def test_401_maps_to_auth_error(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(401, "authError"))
        assert isinstance(mapped, CalendarBackendAuthError)

    def test_403_with_invalid_grant_maps_to_auth_error(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(403, "invalid_grant"))
        assert isinstance(mapped, CalendarBackendAuthError)

    def test_404_maps_to_not_found(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(404, "notFound"))
        assert isinstance(mapped, CalendarBackendNotFoundError)

    def test_412_maps_to_conflict(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(412, ""))
        assert isinstance(mapped, CalendarBackendConflictError)

    def test_429_maps_to_rate_limit(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(429, ""))
        assert isinstance(mapped, CalendarBackendRateLimitError)

    def test_403_with_user_rate_limit_maps_to_rate_limit(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(403, "userRateLimitExceeded"))
        assert isinstance(mapped, CalendarBackendRateLimitError)

    def test_500_maps_to_transient(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(500, ""))
        assert isinstance(mapped, CalendarBackendTransientError)

    def test_socket_timeout_maps_to_transient(self) -> None:

        mapped = GoogleCalendarBackend._map_http_error(TimeoutError("slow"))
        assert isinstance(mapped, CalendarBackendTransientError)

    def test_connection_error_maps_to_transient(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(ConnectionError("nope"))
        assert isinstance(mapped, CalendarBackendTransientError)

    def test_unknown_error_falls_through_to_base(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(RuntimeError("???"))
        assert isinstance(mapped, CalendarBackendError)
        assert not isinstance(mapped, CalendarBackendAuthError)
        assert not isinstance(mapped, CalendarBackendTransientError)

    def test_429_parses_retry_after_header(self) -> None:
        """A 429 with a ``Retry-After`` header must surface
        ``retry_after_sec`` so the service can defer the next poll."""
        mapped = GoogleCalendarBackend._map_http_error(
            _http_error(429, "rateLimitExceeded", headers={"Retry-After": "30"})
        )
        assert isinstance(mapped, CalendarBackendRateLimitError)
        assert mapped.retry_after_sec == 30.0

    def test_429_without_retry_after_leaves_field_none(self) -> None:
        mapped = GoogleCalendarBackend._map_http_error(_http_error(429, ""))
        assert isinstance(mapped, CalendarBackendRateLimitError)
        assert mapped.retry_after_sec is None


# ── Live API surface (mocked) ────────────────────────────────────────


@pytest.mark.asyncio
class TestLiveAPISurface:
    async def test_list_calendars_returns_summary(self) -> None:
        backend, fake = _make_backend()
        fake.calendarList.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "primary",
                    "summary": "Alice's calendar",
                    "timeZone": "America/New_York",
                    "primary": True,
                },
                {
                    "id": "team@example.com",
                    "summary": "Team",
                    "timeZone": "America/New_York",
                    "primary": False,
                },
            ]
        }
        out = await backend.list_calendars()
        assert out == [
            {
                "id": "primary",
                "name": "Alice's calendar",
                "timezone": "America/New_York",
                "primary": True,
            },
            {
                "id": "team@example.com",
                "name": "Team",
                "timezone": "America/New_York",
                "primary": False,
            },
        ]

    async def test_list_calendars_falls_back_to_direct_calendar_lookup(self) -> None:
        backend, fake = _make_backend()
        backend._calendar_id = "alice@example.com"
        fake.calendarList.return_value.list.return_value.execute.return_value = {
            "items": []
        }
        fake.calendars.return_value.get.return_value.execute.return_value = {
            "id": "alice@example.com",
            "summary": "Alice",
            "timeZone": "America/Los_Angeles",
        }

        out = await backend.list_calendars()

        fake.calendars.return_value.get.assert_called_once_with(
            calendarId="alice@example.com",
        )
        assert out == [
            {
                "id": "alice@example.com",
                "name": "Alice",
                "timezone": "America/Los_Angeles",
                "primary": False,
            }
        ]

    async def test_list_events_passes_order_by_start_time(self) -> None:  # noqa: N802
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.list.return_value.execute.return_value = {
            "items": [],
            "timeZone": "UTC",
        }
        time_min = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        time_max = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
        await backend.list_events("primary", time_min, time_max, max_results=50)
        kwargs = events_obj.list.call_args.kwargs
        assert kwargs["orderBy"] == "startTime"
        assert kwargs["singleEvents"] is True
        assert kwargs["maxResults"] == 50
        assert kwargs["calendarId"] == "primary"

    async def test_create_event_passes_request_id_when_idempotency_set(
        self,
    ) -> None:
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.insert.return_value.execute.return_value = {
            "id": "evt_made",
            "summary": "Coffee",
            "start": {"dateTime": "2026-06-01T09:00:00+00:00"},
            "end": {"dateTime": "2026-06-01T09:30:00+00:00"},
            "status": "confirmed",
        }
        req = EventCreateRequest(
            title="Coffee",
            start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
            idempotency_key="abc123",
        )
        evt = await backend.create_event("primary", req)
        kwargs = events_obj.insert.call_args.kwargs
        assert kwargs["calendarId"] == "primary"
        assert kwargs["sendUpdates"] == "none"
        assert kwargs["body"]["requestId"] == "abc123"
        assert evt.event_id == "evt_made"

    async def test_create_event_send_invites_true_uses_send_updates_all(
        self,
    ) -> None:
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.insert.return_value.execute.return_value = {
            "id": "evt_x",
            "summary": "Y",
            "start": {"dateTime": "2026-06-01T09:00:00+00:00"},
            "end": {"dateTime": "2026-06-01T09:30:00+00:00"},
            "status": "confirmed",
        }
        req = EventCreateRequest(
            title="Y",
            start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
            send_invites=True,
        )
        await backend.create_event("primary", req)
        assert events_obj.insert.call_args.kwargs["sendUpdates"] == "all"

    async def test_update_event_translates_412_to_conflict(self) -> None:
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.patch.return_value.execute.side_effect = _http_error(412, "preconditionFailed")
        req = EventCreateRequest(
            title="X",
            start=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        )
        with pytest.raises(CalendarBackendConflictError):
            await backend.update_event("primary", "evt_x", req, if_match_etag="stale")

    async def test_delete_event_send_cancellations_translates_to_send_updates(
        self,
    ) -> None:
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.delete.return_value.execute.return_value = None
        await backend.delete_event("primary", "evt_x", send_cancellations=True)
        assert events_obj.delete.call_args.kwargs["sendUpdates"] == "all"

    async def test_get_event_returns_none_on_404(self) -> None:
        backend, fake = _make_backend()
        events_obj = MagicMock()
        fake.events.return_value = events_obj
        events_obj.get.return_value.execute.side_effect = _http_error(404)
        out = await backend.get_event("primary", "missing")
        assert out is None

    async def test_free_busy_filters_calendars_with_errors(self) -> None:
        backend, fake = _make_backend()
        fake.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {
                "primary": {
                    "busy": [
                        {
                            "start": "2026-05-09T09:00:00+00:00",
                            "end": "2026-05-09T09:30:00+00:00",
                        }
                    ]
                },
                "blocked@example.com": {
                    "errors": [{"reason": "notFound"}],
                },
            }
        }
        out = await backend.free_busy(
            ["primary", "blocked@example.com"],
            datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        )
        ids = [b.calendar_id for b in out]
        assert "primary" in ids
        assert "blocked@example.com" not in ids


# ── Backend registers itself ─────────────────────────────────────────


def test_backend_auto_registers() -> None:
    from gilbert.interfaces.calendar import CalendarBackend

    assert "google_calendar" in CalendarBackend.registered_backends()


def test_backend_config_params_include_required_keys() -> None:
    keys = {p.key for p in GoogleCalendarBackend.backend_config_params()}
    assert keys == {
        "credential_mode",
        "email_address",
        "service_account_json",
        "delegated_user",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_redirect_uri",
        "oauth_refresh_token",
        "oauth_auth_code",
    }


def test_display_name_set() -> None:
    assert GoogleCalendarBackend.display_name == "Google Calendar"
