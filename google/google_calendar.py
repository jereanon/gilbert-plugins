"""Google Calendar backend — service-account JSON + domain-wide delegation.

Mirrors ``gmail.py``: pastes a service-account JSON into config, builds
a Calendar v3 client with ``googleapiclient.discovery.build``, and
wraps every blocking API call in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.interfaces.calendar import (
    AttendeeResponseStatus,
    CalendarAttendee,
    CalendarBackend,
    CalendarBackendAuthError,
    CalendarBackendConflictError,
    CalendarBackendError,
    CalendarBackendNotFoundError,
    CalendarBackendRateLimitError,
    CalendarBackendTransientError,
    CalendarEvent,
    EventCreateRequest,
    EventStatus,
    EventVisibility,
    FreeBusyBlock,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType

from .google_credentials import (
    GoogleCredentialMode,
    build_google_credentials,
    build_google_oauth_authorization_url,
    exchange_google_oauth_code,
    google_credential_spec_from_config,
    require_google_credential_mode,
)

logger = logging.getLogger(__name__)


_AUTH_REASONS = {"authError", "invalid_grant", "forbidden"}
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class GoogleCalendarBackend(CalendarBackend):
    """CalendarBackend backed by Google Calendar v3 via google-api-python-client."""

    backend_name = "google_calendar"
    display_name = "Google Calendar"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="credential_mode",
                type=ToolParameterType.STRING,
                description="Google credential mode. Use oauth_bot for ordinary Google accounts.",
                default=GoogleCredentialMode.OAUTH_BOT.value,
                choices=(
                    GoogleCredentialMode.OAUTH_BOT.value,
                    GoogleCredentialMode.SHARED_SERVICE_ACCOUNT.value,
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT.value,
                ),
                restart_required=True,
            ),
            ConfigParam(
                key="email_address",
                type=ToolParameterType.STRING,
                description="Email address of the calendar owner.",
                restart_required=True,
            ),
            ConfigParam(
                key="service_account_json",
                type=ToolParameterType.STRING,
                description=(
                    "Google service account key (paste JSON content). "
                    "Reuse the same service account configured for "
                    "Gmail if domain-wide delegation is set up; "
                    "otherwise create a dedicated one with calendar "
                    "scopes."
                ),
                sensitive=True,
                restart_required=True,
                multiline=True,
            ),
            ConfigParam(
                key="delegated_user",
                type=ToolParameterType.STRING,
                description=(
                    "Email of the user to impersonate via domain-wide "
                    "delegation."
                ),
                restart_required=True,
            ),
            ConfigParam(
                key="oauth_client_id",
                type=ToolParameterType.STRING,
                description="Google OAuth client ID for oauth_bot mode.",
                restart_required=True,
            ),
            ConfigParam(
                key="oauth_client_secret",
                type=ToolParameterType.STRING,
                description="Google OAuth client secret for oauth_bot mode.",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="oauth_redirect_uri",
                type=ToolParameterType.STRING,
                description="OAuth redirect URI registered for this backend.",
                default="urn:ietf:wg:oauth:2.0:oob",
                restart_required=True,
            ),
            ConfigParam(
                key="oauth_refresh_token",
                type=ToolParameterType.STRING,
                description="OAuth refresh token populated by Connect Google.",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="oauth_auth_code",
                type=ToolParameterType.STRING,
                description="Temporary Google OAuth authorization code for Connect Google complete.",
                sensitive=True,
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="connect_google",
                label="Connect Google",
                description="Open Google's OAuth consent screen for this Calendar backend.",
            ),
            ConfigAction(
                key="connect_google_complete",
                label="Complete Google connection",
                description="Exchange oauth_auth_code for a refresh token.",
            ),
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "List the user's calendars to verify the service account and delegation."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        if key == "connect_google":
            return self._action_connect_google(payload)
        if key == "connect_google_complete":
            return await self._action_connect_google_complete(payload)
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._service is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Google Calendar backend is not initialized — check "
                    "service_account_json and delegated_user, then save "
                    "and restart."
                ),
            )
        try:
            calendars = await self.list_calendars()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Google Calendar API error: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=(f"Connected to Google Calendar — {len(calendars)} calendar(s) accessible."),
        )

    def _action_connect_google(self, payload: dict[str, Any]) -> ConfigActionResult:
        cfg = self._payload_config(payload)
        client_id = str(cfg.get("oauth_client_id") or "")
        redirect_uri = str(cfg.get("oauth_redirect_uri") or "urn:ietf:wg:oauth:2.0:oob")
        if not client_id:
            return ConfigActionResult(status="error", message="oauth_client_id is required before connecting Google.")
        return ConfigActionResult(
            status="pending",
            message="Open Google, approve access, paste the code into oauth_auth_code, then continue.",
            open_url=build_google_oauth_authorization_url(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=self._scopes(),
            ),
            followup_action="connect_google_complete",
        )

    async def _action_connect_google_complete(self, payload: dict[str, Any]) -> ConfigActionResult:
        cfg = self._payload_config(payload)
        auth_code = str(cfg.get("oauth_auth_code") or "")
        if not auth_code:
            return ConfigActionResult(status="error", message="Paste the Google authorization code into oauth_auth_code first.")
        try:
            persist = await exchange_google_oauth_code(
                client_id=str(cfg.get("oauth_client_id") or ""),
                client_secret=str(cfg.get("oauth_client_secret") or ""),
                redirect_uri=str(cfg.get("oauth_redirect_uri") or "urn:ietf:wg:oauth:2.0:oob"),
                auth_code=auth_code,
            )
        except Exception as exc:
            return ConfigActionResult(status="error", message=f"Google OAuth error: {exc}")
        persist["credential_mode"] = GoogleCredentialMode.OAUTH_BOT.value
        return ConfigActionResult(
            status="ok",
            message="Google OAuth refresh token saved into the form. Save to persist it.",
            data={"persist": persist},
        )

    def __init__(self) -> None:
        self._email_address: str = ""
        self._calendar_id: str = ""
        self._service: Any = None

    @staticmethod
    def _scopes() -> tuple[str, ...]:
        return (
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
        )

    @staticmethod
    def _payload_config(payload: dict[str, Any]) -> dict[str, Any]:
        cfg = payload.get("config") if isinstance(payload, dict) else None
        return dict(cfg if isinstance(cfg, dict) else payload)

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        if config is None:
            return
        self._email_address = config.get("email_address", "")
        self._calendar_id = str(config.get("calendar_id", "") or "")
        try:
            from googleapiclient.discovery import build

            spec = google_credential_spec_from_config(
                config,
                scopes=self._scopes(),
                legacy_delegated_user=self._email_address,
            )
            require_google_credential_mode(
                spec,
                supported_modes={
                    GoogleCredentialMode.OAUTH_BOT,
                    GoogleCredentialMode.SHARED_SERVICE_ACCOUNT,
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT,
                },
                backend_label="Google Calendar",
            )
            self._service = await asyncio.to_thread(
                build,
                "calendar",
                "v3",
                credentials=build_google_credentials(spec),
            )
            logger.info(
                "Google Calendar backend initialized (email=%s)",
                self._email_address,
            )
        except Exception:
            logger.exception("Failed to initialize Google Calendar backend")

    async def close(self) -> None:
        self._service = None

    def _ensure_service(self) -> Any:
        if self._service is None:
            raise CalendarBackendAuthError(
                "Google Calendar backend not initialized — check service_account_json"
            )
        return self._service

    # ── Mapping helpers ──────────────────────────────────────────────

    @staticmethod
    def _resolve_zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @classmethod
    def _parse_event_dt(cls, raw: dict[str, Any], default_tz: str = "UTC") -> tuple[datetime, bool]:
        """Return (tz-aware datetime, is_all_day)."""
        if "dateTime" in raw:
            dt = datetime.fromisoformat(str(raw["dateTime"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                tz_name = str(raw.get("timeZone") or default_tz)
                dt = dt.replace(tzinfo=cls._resolve_zone(tz_name))
            return dt, False
        if "date" in raw:
            d = datetime.fromisoformat(str(raw["date"]))
            tz_name = str(raw.get("timeZone") or default_tz)
            return d.replace(tzinfo=cls._resolve_zone(tz_name)), True
        # Fallback — treat as now in UTC.
        return datetime.now(UTC), False

    @classmethod
    def _to_calendar_event(
        cls,
        data: dict[str, Any],
        *,
        account_id: str,
        calendar_id: str,
        default_tz: str = "UTC",
    ) -> CalendarEvent:
        start_raw = data.get("start") or {}
        end_raw = data.get("end") or {}
        start, all_day_start = cls._parse_event_dt(start_raw, default_tz)
        end, all_day_end = cls._parse_event_dt(end_raw, default_tz)
        all_day = bool(all_day_start or all_day_end)
        # Normalise all-day end to next-midnight semantics: Google
        # returns the date AFTER the last day, so an all-day event on
        # 2026-05-09 has start.date=2026-05-09 / end.date=2026-05-10.
        # No transformation needed here; downstream uses [start, end).
        if start == end:
            logger.warning(
                "google_calendar event has start == end (id=%s); defaulting end = start + 1h",
                data.get("id", ""),
            )
            end = start + timedelta(hours=1)
        attendees_raw = data.get("attendees") or []
        attendees: list[CalendarAttendee] = []
        for raw in attendees_raw:
            try:
                status = AttendeeResponseStatus(str(raw.get("responseStatus") or "needsAction"))
            except ValueError:
                status = AttendeeResponseStatus.NEEDS_ACTION
            attendees.append(
                CalendarAttendee(
                    email=str(raw.get("email", "")),
                    name=str(raw.get("displayName", "")),
                    response_status=status,
                    is_organizer=bool(raw.get("organizer", False)),
                    is_self=bool(raw.get("self", False)),
                )
            )
        try:
            visibility = EventVisibility(
                str(data.get("visibility") or EventVisibility.DEFAULT.value)
            )
        except ValueError:
            visibility = EventVisibility.DEFAULT
        try:
            status = EventStatus(str(data.get("status") or EventStatus.CONFIRMED.value))
        except ValueError:
            status = EventStatus.CONFIRMED
        return CalendarEvent(
            event_id=str(data.get("id", "")),
            calendar_id=calendar_id,
            account_id=account_id,
            title=str(data.get("summary", "(no title)")),
            start=start,
            end=end,
            etag=str(data.get("etag", "")),
            all_day=all_day,
            description=str(data.get("description", "")),
            location=str(data.get("location", "")),
            organizer_email=str((data.get("organizer") or {}).get("email", "")),
            attendees=tuple(attendees),
            visibility=visibility,
            status=status,
            transparency=str(data.get("transparency", "opaque")),
            html_link=str(data.get("htmlLink", "")),
            recurring_event_id=(
                str(data["recurringEventId"]) if data.get("recurringEventId") else None
            ),
        )

    @staticmethod
    def _request_to_body(request: EventCreateRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"summary": request.title}
        if request.description:
            body["description"] = request.description
        if request.location:
            body["location"] = request.location
        if request.visibility != EventVisibility.DEFAULT:
            body["visibility"] = request.visibility.value
        if request.all_day:
            body["start"] = {"date": request.start.date().isoformat()}
            body["end"] = {"date": request.end.date().isoformat()}
        else:
            body["start"] = {
                "dateTime": request.start.isoformat(),
                "timeZone": str(request.start.tzinfo)
                if request.start.tzinfo is not None
                else "UTC",
            }
            body["end"] = {
                "dateTime": request.end.isoformat(),
                "timeZone": str(request.end.tzinfo) if request.end.tzinfo is not None else "UTC",
            }
        if request.attendees:
            body["attendees"] = [
                {"email": a.email, "displayName": a.name} for a in request.attendees
            ]
        return body

    # ── Error mapping ────────────────────────────────────────────────

    @staticmethod
    def _map_http_error(exc: Exception) -> CalendarBackendError:
        """Translate googleapiclient.errors.HttpError → typed backend error."""
        try:
            from googleapiclient.errors import HttpError as _HttpError  # noqa: N813
        except ImportError:
            _HttpError = None  # type: ignore[assignment,misc]  # noqa: N806
        if isinstance(exc, (socket.timeout, ConnectionError, ssl.SSLError)):
            return CalendarBackendTransientError(str(exc))
        if _HttpError is not None and isinstance(exc, _HttpError):
            status = getattr(getattr(exc, "resp", None), "status", 0)
            reason = ""
            try:
                content = json.loads(
                    exc.content.decode("utf-8")
                    if isinstance(exc.content, (bytes, bytearray))
                    else str(exc.content)
                )
                errors = (
                    content.get("error", {}).get("errors", []) if isinstance(content, dict) else []
                )
                if errors:
                    reason = str(errors[0].get("reason", ""))
            except Exception:
                reason = ""
            if status == 401 or (status == 403 and reason in _AUTH_REASONS):
                return CalendarBackendAuthError(str(exc))
            if status == 404:
                return CalendarBackendNotFoundError(str(exc))
            if status == 412:
                return CalendarBackendConflictError(str(exc))
            if status == 429 or (status == 403 and reason in _RATE_LIMIT_REASONS):
                retry_after = None
                try:
                    headers = getattr(getattr(exc, "resp", None), "headers", {}) or {}
                    for k, v in headers.items():
                        if k.lower() == "retry-after":
                            retry_after = float(v)
                            break
                except Exception:
                    retry_after = None
                return CalendarBackendRateLimitError(str(exc), retry_after_sec=retry_after)
            if status >= 500:
                return CalendarBackendTransientError(str(exc))
        return CalendarBackendError(str(exc))

    async def _exec_with_mapping(self, fn: Any) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            mapped = self._map_http_error(exc)
            raise mapped from exc

    # ── ABC methods ──────────────────────────────────────────────────

    async def list_calendars(self) -> list[dict[str, Any]]:
        svc = self._ensure_service()
        result = await self._exec_with_mapping(
            svc.calendarList().list().execute,
        )
        items = result.get("items", []) or []
        calendars = [
            {
                "id": str(c.get("id", "")),
                "name": str(c.get("summary", "")),
                "timezone": str(c.get("timeZone", "UTC")),
                "primary": bool(c.get("primary", False)),
            }
            for c in items
        ]
        if calendars:
            return calendars

        fallback_id = self._calendar_id or self._email_address
        if not fallback_id:
            return []
        cal = await self._exec_with_mapping(
            svc.calendars().get(calendarId=fallback_id).execute,
        )
        return [
            {
                "id": str(cal.get("id", fallback_id)),
                "name": str(cal.get("summary", fallback_id)),
                "timezone": str(cal.get("timeZone", "UTC")),
                "primary": False,
            }
        ]

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        max_results: int = 250,
        single_events: bool = True,
    ) -> list[CalendarEvent]:
        svc = self._ensure_service()
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("time_min and time_max must be tz-aware")

        def _go() -> dict[str, Any]:
            return (
                svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min.astimezone(UTC).isoformat(),
                    timeMax=time_max.astimezone(UTC).isoformat(),
                    singleEvents=single_events,
                    orderBy="startTime",
                    maxResults=max_results,
                )
                .execute()
            )

        result = await self._exec_with_mapping(_go)
        items = result.get("items", []) or []
        default_tz = result.get("timeZone", "UTC")
        return [
            self._to_calendar_event(
                item,
                account_id="",
                calendar_id=calendar_id,
                default_tz=default_tz,
            )
            for item in items
        ]

    async def get_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> CalendarEvent | None:
        svc = self._ensure_service()
        try:
            data = await self._exec_with_mapping(
                svc.events().get(calendarId=calendar_id, eventId=event_id).execute,
            )
        except CalendarBackendNotFoundError:
            return None
        return self._to_calendar_event(
            data,
            account_id="",
            calendar_id=calendar_id,
        )

    async def free_busy(
        self,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> list[FreeBusyBlock]:
        svc = self._ensure_service()
        body = {
            "timeMin": time_min.astimezone(UTC).isoformat(),
            "timeMax": time_max.astimezone(UTC).isoformat(),
            "items": [{"id": cid} for cid in calendar_ids],
        }
        result = await self._exec_with_mapping(
            svc.freebusy().query(body=body).execute,
        )
        blocks: list[FreeBusyBlock] = []
        cals = result.get("calendars", {}) or {}
        for cid, payload in cals.items():
            if not isinstance(payload, dict):
                continue
            errs = payload.get("errors") or []
            if errs:
                logger.info(
                    "free_busy errors for %s: %s",
                    cid,
                    [e.get("reason") for e in errs],
                )
                continue
            for b in payload.get("busy", []) or []:
                try:
                    s = datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))
                    e = datetime.fromisoformat(str(b["end"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                blocks.append(FreeBusyBlock(calendar_id=cid, start=s, end=e))
        return blocks

    async def create_event(
        self,
        calendar_id: str,
        request: EventCreateRequest,
    ) -> CalendarEvent:
        svc = self._ensure_service()
        body = self._request_to_body(request)
        if request.idempotency_key:
            body["requestId"] = request.idempotency_key

        def _go() -> dict[str, Any]:
            return (
                svc.events()
                .insert(
                    calendarId=calendar_id,
                    sendUpdates="all" if request.send_invites else "none",
                    body=body,
                )
                .execute()
            )

        data = await self._exec_with_mapping(_go)
        return self._to_calendar_event(data, account_id="", calendar_id=calendar_id)

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        request: EventCreateRequest,
        *,
        if_match_etag: str = "",
    ) -> CalendarEvent:
        svc = self._ensure_service()
        body = self._request_to_body(request)

        def _go() -> dict[str, Any]:
            patch = svc.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates="all" if request.send_invites else "none",
            )
            if if_match_etag:
                # Set If-Match on the underlying HTTP request. If the
                # google-api-python-client request shape ever changes,
                # log loudly so an OCC silently-broken regression is
                # visible during smoke tests instead of producing
                # silent last-write-wins behaviour.
                try:
                    patch.headers["If-Match"] = if_match_etag  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.warning(
                        "google_calendar: failed to set If-Match header — "
                        "OCC may not be enforced for this update (cause: %s)",
                        exc,
                    )
            return patch.execute()

        data = await self._exec_with_mapping(_go)
        return self._to_calendar_event(data, account_id="", calendar_id=calendar_id)

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        send_cancellations: bool = False,
    ) -> None:
        svc = self._ensure_service()

        def _go() -> Any:
            return (
                svc.events()
                .delete(
                    calendarId=calendar_id,
                    eventId=event_id,
                    sendUpdates="all" if send_cancellations else "none",
                )
                .execute()
            )

        await self._exec_with_mapping(_go)
