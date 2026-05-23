"""Google Tasks backend — service-account JSON + domain-wide delegation.

Mirrors ``gmail.py`` and ``google_calendar.py``: pastes a service-account
JSON into config, builds a Tasks v1 client with
``googleapiclient.discovery.build``, and wraps every blocking API call
in ``asyncio.to_thread``.

Limitations (documented in spec §11.6):

- DWD requires Google Workspace — personal ``gmail.com`` accounts
  cannot use this backend until the per-user OAuth flow ships.
- One Gilbert list = one Google ``tasklist`` (bind via ``tasklist_id``).
- The service account's client ID must have the
  ``https://www.googleapis.com/auth/tasks`` scope authorized in the
  Workspace admin console.
- No webhook surface — sync is poll-only, the service uses
  ``updatedMin`` for delta semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tasks import (
    Task,
    TaskBackend,
    TaskBackendAuthError,
    TaskBackendError,
    TaskBackendNotFoundError,
    TaskBackendRateLimitError,
    TaskBackendTransientError,
    TaskStatus,
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


class GoogleTasksBackend(TaskBackend):
    """``TaskBackend`` backed by Google Tasks v1 via google-api-python-client."""

    backend_name = "google_tasks"

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
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT.value,
                    GoogleCredentialMode.SHARED_SERVICE_ACCOUNT.value,
                ),
                restart_required=True,
            ),
            ConfigParam(
                key="service_account_json",
                type=ToolParameterType.STRING,
                description=(
                    "Google service account key (paste JSON content). "
                    "Reuse the same service account configured for "
                    "Gmail / Calendar if domain-wide delegation is set "
                    "up; the admin must additionally grant the "
                    "https://www.googleapis.com/auth/tasks scope to the "
                    "service account's client ID."
                ),
                sensitive=True,
                restart_required=True,
                multiline=True,
            ),
            ConfigParam(
                key="delegated_user",
                type=ToolParameterType.STRING,
                description=(
                    "Email of the user to impersonate via DWD. Required "
                    "for the Tasks API; without it the backend will "
                    "raise on initialize."
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
            ConfigParam(
                key="tasklist_id",
                type=ToolParameterType.STRING,
                description=(
                    "Google tasklist id to bind this Gilbert list to. "
                    "Use 'Show available tasklists' first to fetch the "
                    "id of the tasklist you want."
                ),
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="connect_google",
                label="Connect Google",
                description="Open Google's OAuth consent screen for this Tasks backend.",
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
                    "List your Google Tasks lists to verify the service "
                    "account, delegation, and scope authorization."
                ),
            ),
            ConfigAction(
                key="list_tasklists",
                label="Show available tasklists",
                description=(
                    "Return the id and title of every tasklist on the "
                    "delegated user's account."
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
        if key == "list_tasklists":
            return await self._action_list_tasklists()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._service is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Google Tasks backend is not initialized — check "
                    "service_account_json + delegated_user, save, and "
                    "restart."
                ),
            )
        try:
            tasklists = await self.list_tasklists()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Google Tasks API error: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to Google Tasks — {len(tasklists)} tasklist(s) "
                "accessible."
            ),
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

    async def _action_list_tasklists(self) -> ConfigActionResult:
        if self._service is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Google Tasks backend is not initialized — check "
                    "service_account_json + delegated_user, save, and "
                    "restart."
                ),
            )
        try:
            tasklists = await self.list_tasklists()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Google Tasks API error: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=(
                f"Found {len(tasklists)} tasklist(s):\n"
                + "\n".join(
                    f"  {tl['id']}\t{tl['title']}" for tl in tasklists
                )
            ),
            data={"tasklists": tasklists},
        )

    def __init__(self) -> None:
        self._tasklist_id: str = ""
        self._service: Any = None  # googleapiclient resource

    @staticmethod
    def _scopes() -> tuple[str, ...]:
        return ("https://www.googleapis.com/auth/tasks",)

    @staticmethod
    def _payload_config(payload: dict[str, Any]) -> dict[str, Any]:
        cfg = payload.get("config") if isinstance(payload, dict) else None
        return dict(cfg if isinstance(cfg, dict) else payload)

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        if config is None:
            return
        self._tasklist_id = str(config.get("tasklist_id", ""))
        try:
            from googleapiclient.discovery import build

            spec = google_credential_spec_from_config(
                config,
                scopes=self._scopes(),
            )
            require_google_credential_mode(
                spec,
                supported_modes={
                    GoogleCredentialMode.OAUTH_BOT,
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT,
                },
                backend_label="Google Tasks",
            )
            self._service = await asyncio.to_thread(
                build,
                "tasks",
                "v1",
                credentials=build_google_credentials(spec),
            )
            logger.info(
                "Google Tasks backend initialized (tasklist=%s, user=%s)",
                self._tasklist_id,
                spec.delegated_user or "(oauth)",
            )
        except ValueError as exc:
            raise TaskBackendAuthError(str(exc)) from exc
        except Exception:
            logger.exception("Failed to initialize Google Tasks backend")
            raise

    async def close(self) -> None:
        self._service = None

    def _ensure_service(self) -> Any:
        if self._service is None:
            raise TaskBackendAuthError(
                "Google Tasks backend not initialized — check "
                "service_account_json"
            )
        return self._service

    # ── Mapping helpers ──────────────────────────────────────────────

    @staticmethod
    def _normalize_iso_z(raw: str) -> str:
        """Normalize an RFC3339 timestamp (UTC) to ISO with trailing 'Z'."""
        if not raw:
            return ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _to_task(
        cls, data: dict[str, Any], *, project: str = ""
    ) -> Task:
        status_raw = str(data.get("status") or "needsAction")
        status = (
            TaskStatus.DONE if status_raw == "completed" else TaskStatus.OPEN
        )
        return Task(
            id="",
            list_id="",
            source_id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            notes=str(data.get("notes", "")),
            due_at=cls._normalize_iso_z(str(data.get("due", ""))),
            # Google Tasks ``due`` is always day-precision UTC midnight
            # in practice — surface UTC as the authoring zone so day
            # boundary math is at least correct against UTC.
            due_at_tz="UTC" if data.get("due") else "",
            completed_at=cls._normalize_iso_z(
                str(data.get("completed", ""))
            ),
            status=status,
            project=project,
            updated_at=cls._normalize_iso_z(str(data.get("updated", ""))),
            etag=str(data.get("etag", "")),
        )

    @staticmethod
    def _task_to_body(task: Task) -> dict[str, Any]:
        body: dict[str, Any] = {"title": task.title}
        if task.notes:
            body["notes"] = task.notes
        if task.due_at:
            body["due"] = task.due_at
        if task.status == TaskStatus.DONE:
            body["status"] = "completed"
        return body

    @staticmethod
    def _patch_to_body(patch: dict[str, Any]) -> dict[str, Any]:
        """Convert a Gilbert patch to a Google Tasks PATCH body.

        Only translates fields Google Tasks supports natively. Patch
        keys not in the mapping are silently dropped at the backend
        boundary — Gilbert's local row already holds them, so dropping
        the upstream push is safe.
        """
        body: dict[str, Any] = {}
        if "title" in patch:
            body["title"] = str(patch["title"])
        if "notes" in patch:
            body["notes"] = str(patch["notes"])
        if "due_at" in patch:
            body["due"] = str(patch["due_at"])
        return body

    # ── Error mapping ────────────────────────────────────────────────

    @staticmethod
    def _map_http_error(exc: Exception) -> TaskBackendError:
        try:
            from googleapiclient.errors import HttpError as _HttpError  # noqa: N813
        except ImportError:
            _HttpError = None  # type: ignore[assignment,misc]  # noqa: N806
        if isinstance(exc, (socket.timeout, ConnectionError, ssl.SSLError, TimeoutError)):
            return TaskBackendTransientError(str(exc))
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
                    content.get("error", {}).get("errors", [])
                    if isinstance(content, dict)
                    else []
                )
                if errors:
                    reason = str(errors[0].get("reason", ""))
            except Exception:
                reason = ""
            if status == 401 or (status == 403 and reason in _AUTH_REASONS):
                return TaskBackendAuthError(str(exc))
            if status == 404:
                return TaskBackendNotFoundError(str(exc))
            if status == 429 or (
                status == 403 and reason in _RATE_LIMIT_REASONS
            ):
                retry_after: float | None = None
                try:
                    headers = (
                        getattr(getattr(exc, "resp", None), "headers", {}) or {}
                    )
                    for k, v in headers.items():
                        if k.lower() == "retry-after":
                            retry_after = float(v)
                            break
                except Exception:
                    retry_after = None
                return TaskBackendRateLimitError(
                    str(exc), retry_after_sec=retry_after
                )
            if status >= 500:
                return TaskBackendTransientError(str(exc))
        return TaskBackendError(str(exc))

    async def _exec_with_mapping(self, fn: Any) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            mapped = self._map_http_error(exc)
            raise mapped from exc

    # ── Tasklists ────────────────────────────────────────────────────

    async def list_tasklists(self) -> list[dict[str, Any]]:
        svc = self._ensure_service()
        result = await self._exec_with_mapping(
            svc.tasklists().list().execute,
        )
        items = result.get("items", []) or []
        return [
            {"id": str(t.get("id", "")), "title": str(t.get("title", ""))}
            for t in items
        ]

    def supports_projects(self) -> bool:
        # Each Google tasklist behaves like one project; binding is
        # one-to-one so v1 doesn't surface a project picker.
        return False

    # ── ABC: list_tasks ──────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        include_completed: bool = False,
        updated_since: str = "",
    ) -> list[Task]:
        svc = self._ensure_service()
        if not self._tasklist_id:
            raise TaskBackendError(
                "tasklist_id is not configured for this Gilbert list"
            )

        # Resolve the tasklist title once for the project field.
        project = ""
        try:
            meta = await self._exec_with_mapping(
                svc.tasklists().get(tasklist=self._tasklist_id).execute,
            )
            project = str(meta.get("title", ""))
        except Exception:
            # Non-fatal — the service can still operate without the
            # project label.
            pass

        results: list[Task] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "tasklist": self._tasklist_id,
                "showCompleted": include_completed,
                "showHidden": False,
            }
            if updated_since:
                params["updatedMin"] = updated_since
            if page_token:
                params["pageToken"] = page_token

            def _go(p: dict[str, Any] = params) -> dict[str, Any]:
                return svc.tasks().list(**p).execute()

            data = await self._exec_with_mapping(_go)
            for item in data.get("items", []) or []:
                results.append(self._to_task(item, project=project))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return results

    async def add_task(self, task: Task) -> Task:
        svc = self._ensure_service()
        if not self._tasklist_id:
            raise TaskBackendError(
                "tasklist_id is not configured for this Gilbert list"
            )
        body = self._task_to_body(task)

        def _go() -> dict[str, Any]:
            return (
                svc.tasks()
                .insert(tasklist=self._tasklist_id, body=body)
                .execute()
            )

        data = await self._exec_with_mapping(_go)
        return self._to_task(data)

    async def update_task(
        self,
        source_id: str,
        patch: dict[str, Any],
        *,
        etag: str = "",
    ) -> Task:
        svc = self._ensure_service()
        if not self._tasklist_id:
            raise TaskBackendError(
                "tasklist_id is not configured for this Gilbert list"
            )
        body = self._patch_to_body(patch)

        def _go() -> dict[str, Any]:
            return (
                svc.tasks()
                .patch(
                    tasklist=self._tasklist_id,
                    task=source_id,
                    body=body,
                )
                .execute()
            )

        data = await self._exec_with_mapping(_go)
        return self._to_task(data)

    async def complete_task(self, source_id: str) -> None:
        svc = self._ensure_service()
        if not self._tasklist_id:
            raise TaskBackendError(
                "tasklist_id is not configured for this Gilbert list"
            )

        def _go() -> dict[str, Any]:
            return (
                svc.tasks()
                .patch(
                    tasklist=self._tasklist_id,
                    task=source_id,
                    body={"status": "completed"},
                )
                .execute()
            )

        try:
            await self._exec_with_mapping(_go)
        except TaskBackendNotFoundError:
            # Naturally idempotent — if the row is already gone upstream
            # there's nothing to complete.
            return
        except TaskBackendError as exc:
            # Some providers reject completing an already-completed
            # task with a 4xx; swallow rather than retry.
            msg = str(exc).lower()
            if "already" in msg or "completed" in msg:
                return
            raise

    async def delete_task(self, source_id: str) -> None:
        svc = self._ensure_service()
        if not self._tasklist_id:
            raise TaskBackendError(
                "tasklist_id is not configured for this Gilbert list"
            )

        def _go() -> Any:
            return (
                svc.tasks()
                .delete(tasklist=self._tasklist_id, task=source_id)
                .execute()
            )

        try:
            await self._exec_with_mapping(_go)
        except TaskBackendNotFoundError:
            # Naturally idempotent — already gone is success.
            return
