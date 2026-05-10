"""Withings Public Cloud OAuth pull backend.

OAuth 2.0 authorization-code grant. Per-user state (``access_token``,
``refresh_token``, ``oauth_user_id``, ``oauth_expires_at``,
``last_sync_at``, cursor) lives on the ``health_links`` row; global
``client_id`` / ``client_secret`` come from the backend's config.

Token refresh on 401: the backend retries the request once after
refreshing. A 401 on the refresh itself raises
``HealthBackendAuthError`` so the service surfaces a "reconnect"
prompt and (after 5 consecutive auth failures) auto-disables the
link row.

Disconnect overrides the default to call Withings's
``/oauth2/revoke`` BEFORE the local row is dropped — the user's
"I disconnected" intent revokes upstream access. Revocation failure
logs WARN but does NOT block local cleanup.

Per spec §6.4 OAuth tokens stay PLAINTEXT in v1 (encryption at rest
via OS-keychain Fernet is v2 work). The plugin's account panel
surfaces a "Tokens stored unencrypted on this Gilbert instance until
v2." disclosure so the user can make an informed choice.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.health import (
    HealthBackend,
    HealthBackendAuthError,
    HealthBackendError,
    HealthBackendRateLimitError,
    HealthBackendTransientError,
    HealthMetric,
    LinkCompleteResult,
    LinkStartResult,
    MetricType,
    MetricUnit,
)
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
_API_BASE = "https://wbsapi.withings.net"
_OAUTH_REVOKE_ACTION = "revoke"
_OAUTH_STATE_COLLECTION = "health_oauth_state"
_LINKS_COLLECTION = "health_links"

# Withings ``getmeas`` ``meastype`` field — see
# https://developer.withings.com/api-reference/#tag/measure
_MEASTYPE_MAP: dict[int, tuple[MetricType, MetricUnit]] = {
    1: (MetricType.WEIGHT, MetricUnit.KG),
    5: (MetricType.LEAN_MASS, MetricUnit.KG),
    6: (MetricType.BODY_FAT, MetricUnit.PERCENT),
    8: (MetricType.WEIGHT, MetricUnit.KG),
    9: (MetricType.BLOOD_PRESSURE_DIA, MetricUnit.MMHG),
    10: (MetricType.BLOOD_PRESSURE_SYS, MetricUnit.MMHG),
    11: (MetricType.HEART_RATE_AVG, MetricUnit.BPM),
    12: (MetricType.BODY_TEMPERATURE, MetricUnit.CELSIUS),
    54: (MetricType.SPO2, MetricUnit.PERCENT),
    73: (MetricType.BODY_TEMPERATURE, MetricUnit.CELSIUS),
    77: (MetricType.HEART_RATE_RESTING, MetricUnit.BPM),
}


class WithingsBackend(HealthBackend):
    backend_name = "withings"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_id: str = ""
        self._client_secret: str = ""
        self._storage: StorageBackend | None = None
        self._public_base_url: str = ""
        # Per-user refresh-lock: two concurrent ``_call`` paths for
        # the same user MUST NOT both POST ``refresh_token`` to
        # Withings (the second exchange is rejected because Withings
        # invalidates the prior refresh token on first use, which
        # cascades into ``HealthBackendAuthError`` and after 5
        # consecutive failures auto-disables the user's link). The
        # lock dict is per-instance: backend instances are singletons
        # within the service, so the locks fan out by user_id.
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    # ── StorageAwareHealthBackend ────────────────────────────────────

    def set_storage(self, storage: object) -> None:
        if isinstance(storage, StorageBackend):
            self._storage = storage

    def set_public_base_url(self, url: str) -> None:
        self._public_base_url = url

    # ── Capability flags ─────────────────────────────────────────────

    @property
    def supports_pull(self) -> bool:
        return True

    # ── Config ───────────────────────────────────────────────────────

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="client_id",
                type=ToolParameterType.STRING,
                description=(
                    "Withings developer-app client ID. Register the app "
                    "at https://developer.withings.com/."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="client_secret",
                type=ToolParameterType.STRING,
                description="Withings developer-app client secret.",
                default="",
                sensitive=True,
            ),
        ]

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, Any]) -> None:
        self._client_id = str(config.get("client_id") or "")
        self._client_secret = str(config.get("client_secret") or "")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── OAuth flow ───────────────────────────────────────────────────

    def _callback_url(self) -> str:
        base = (self._public_base_url or "").rstrip("/")
        if not base:
            return ""
        return f"{base}/api/health/me/oauth/withings/callback"

    async def begin_link(self, user_id: str) -> LinkStartResult:
        if not self._client_id or not self._client_secret:
            return LinkStartResult(
                status="error",
                message=(
                    "Admin needs to set Withings client_id / "
                    "client_secret in Settings → Personal Data → "
                    "Health before users can connect."
                ),
            )
        callback = self._callback_url()
        if not callback:
            return LinkStartResult(
                status="error",
                message=(
                    "Admin needs to set gilbert.public_base_url before "
                    "Withings can be connected."
                ),
            )
        if self._storage is None:
            return LinkStartResult(
                status="error",
                message="storage unavailable",
            )

        state = secrets.token_urlsafe(24)
        await self._storage.put(
            _OAUTH_STATE_COLLECTION,
            state,
            {
                "_id": state,
                "user_id": user_id,
                "backend_name": self.backend_name,
                "created_at": datetime.now(UTC).isoformat(),
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=600)
                ).isoformat(),
            },
        )
        from urllib.parse import urlencode

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": callback,
            "scope": "user.metrics,user.activity,user.sleepevents",
            "state": state,
        }
        return LinkStartResult(
            status="ok",
            open_url=f"{_AUTHORIZE_URL}?{urlencode(params)}",
        )

    async def complete_link(
        self,
        user_id: str,
        payload: dict[str, Any],
    ) -> LinkCompleteResult:
        if self._client is None or self._storage is None:
            return LinkCompleteResult(status="error", message="not initialized")
        code = str(payload.get("code") or "")
        if not code:
            return LinkCompleteResult(status="error", message="missing code")
        callback = self._callback_url()
        if not callback:
            return LinkCompleteResult(
                status="error",
                message="public_base_url not set",
            )
        body = {
            "action": "requesttoken",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback,
        }
        try:
            response = await self._client.post(_TOKEN_URL, data=body)
            response.raise_for_status()
            json_body = response.json()
        except httpx.HTTPError as exc:
            return LinkCompleteResult(
                status="error",
                message=f"token exchange failed: {exc}",
            )
        if json_body.get("status") != 0:
            return LinkCompleteResult(
                status="error",
                message=f"withings error: {json_body.get('error') or json_body!r}",
            )

        body_data = json_body.get("body") or {}
        access_token = str(body_data.get("access_token") or "")
        refresh_token = str(body_data.get("refresh_token") or "")
        oauth_user_id = str(body_data.get("userid") or "")
        expires_in = int(body_data.get("expires_in") or 0)
        if not access_token or not refresh_token:
            return LinkCompleteResult(
                status="error",
                message="withings: missing tokens",
            )

        link_id = f"{user_id}/{self.backend_name}"
        existing = await self._storage.get(_LINKS_COLLECTION, link_id) or {}
        existing.update(
            {
                "_id": link_id,
                "user_id": user_id,
                "backend_name": self.backend_name,
                "enabled": True,
                "oauth_access_token": access_token,
                "oauth_refresh_token": refresh_token,
                "oauth_user_id": oauth_user_id,
                "oauth_expires_at": (
                    datetime.now(UTC) + timedelta(seconds=expires_in)
                ).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        existing.setdefault("created_at", datetime.now(UTC).isoformat())
        await self._storage.put(_LINKS_COLLECTION, link_id, existing)
        return LinkCompleteResult(status="ok")

    async def disconnect(self, user_id: str) -> None:
        """Revoke upstream BEFORE deleting the local row.

        Failure to revoke logs WARN but local cleanup proceeds — the
        user's "I disconnected" intent must succeed locally even if
        Withings is unreachable.
        """
        if self._storage is None:
            return None
        link_id = f"{user_id}/{self.backend_name}"
        link = await self._storage.get(_LINKS_COLLECTION, link_id)
        if link is None:
            return None
        access_token = str(link.get("oauth_access_token") or "")
        if access_token and self._client is not None:
            try:
                response = await self._client.post(
                    _TOKEN_URL,
                    data={
                        "action": _OAUTH_REVOKE_ACTION,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "userid": link.get("oauth_user_id") or "",
                    },
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "withings: upstream revoke failed for %s: %s — local "
                    "cleanup proceeds",
                    user_id,
                    exc,
                )
        # The HealthService deletes the local row after we return.

    # ── Sync ─────────────────────────────────────────────────────────

    async def sync(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
    ) -> list[HealthMetric]:
        if self._client is None or self._storage is None:
            raise HealthBackendError("withings: not initialized")
        link_id = f"{user_id}/{self.backend_name}"
        link = await self._storage.get(_LINKS_COLLECTION, link_id)
        if link is None or not link.get("oauth_access_token"):
            raise HealthBackendAuthError("withings: not linked")

        cursor = since or self._cursor_from_link(link)
        out: list[HealthMetric] = []
        out.extend(await self._sync_meas(user_id, link, cursor))
        out.extend(await self._sync_sleep(user_id, link, cursor))
        # Persist the new cursor (max recorded_at across this run).
        if out:
            new_cursor = max(m.recorded_at for m in out)
            link["lastupdate"] = int(new_cursor.timestamp())
            link["last_sync_at"] = datetime.now(UTC).isoformat()
            await self._storage.put(_LINKS_COLLECTION, link_id, link)
        return out

    def _cursor_from_link(self, link: dict[str, Any]) -> datetime:
        cursor_raw = link.get("lastupdate")
        if isinstance(cursor_raw, int) and cursor_raw > 0:
            return datetime.fromtimestamp(cursor_raw, tz=UTC)
        return datetime.now(UTC) - timedelta(days=14)

    async def _sync_meas(
        self,
        user_id: str,
        link: dict[str, Any],
        since: datetime,
    ) -> list[HealthMetric]:
        json_body = await self._call(
            link,
            "/measure",
            {
                "action": "getmeas",
                "lastupdate": int(since.timestamp()),
            },
        )
        out: list[HealthMetric] = []
        groups = json_body.get("body", {}).get("measuregrps") or []
        for grp in groups:
            recorded_at = datetime.fromtimestamp(int(grp.get("date") or 0), tz=UTC)
            grpid = str(grp.get("grpid") or "")
            for m in grp.get("measures") or []:
                meastype = int(m.get("type") or 0)
                spec = _MEASTYPE_MAP.get(meastype)
                if spec is None:
                    continue
                metric_type, unit = spec
                value_raw = m.get("value")
                unit_exp = int(m.get("unit") or 0)
                if value_raw is None:
                    continue
                try:
                    value = float(value_raw) * (10**unit_exp)
                except (TypeError, ValueError):
                    continue
                # Withings body fat is a percentage (0-100); the
                # MetricUnit.PERCENT contract is 0.0..1.0.
                if unit is MetricUnit.PERCENT:
                    value = value / 100.0
                out.append(
                    HealthMetric(
                        id="",
                        user_id=user_id,
                        backend=self.backend_name,
                        metric_type=metric_type,
                        value=value,
                        unit=unit,
                        recorded_at=recorded_at,
                        ingested_at=datetime.now(UTC),
                        source_event_id=f"meas:{grpid}:{meastype}",
                        extra={"measure_grpid": grpid},
                    )
                )
        return out

    async def _sync_sleep(
        self,
        user_id: str,
        link: dict[str, Any],
        since: datetime,
    ) -> list[HealthMetric]:
        json_body = await self._call(
            link,
            "/v2/sleep",
            {
                "action": "getsummary",
                "lastupdate": int(since.timestamp()),
                "data_fields": "totalsleepduration,deepsleepduration,remsleepduration,wakeupduration",
            },
        )
        out: list[HealthMetric] = []
        series = json_body.get("body", {}).get("series") or []
        for s in series:
            data = s.get("data") or {}
            recorded_at = datetime.fromtimestamp(int(s.get("startdate") or 0), tz=UTC)
            sid = str(s.get("id") or "")
            mapping = {
                "totalsleepduration": MetricType.SLEEP_DURATION,
                "deepsleepduration": MetricType.SLEEP_DEEP,
                "remsleepduration": MetricType.SLEEP_REM,
                "wakeupduration": MetricType.SLEEP_AWAKE,
            }
            for field, metric_type in mapping.items():
                raw = data.get(field)
                if raw is None:
                    continue
                try:
                    seconds = float(raw)
                except (TypeError, ValueError):
                    continue
                out.append(
                    HealthMetric(
                        id="",
                        user_id=user_id,
                        backend=self.backend_name,
                        metric_type=metric_type,
                        value=seconds,
                        unit=MetricUnit.SECONDS,
                        recorded_at=recorded_at,
                        ingested_at=datetime.now(UTC),
                        source_event_id=f"sleep:{sid}:{field}",
                    )
                )
        return out

    async def _call(
        self,
        link: dict[str, Any],
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a Withings endpoint, refreshing the token once on 401.

        The refresh path is serialized per-user via
        ``self._refresh_locks[user_id]``: two concurrent callers
        seeing 401 would otherwise both POST refresh_token to
        Withings, the second exchange fails (refresh token
        invalidated), and the user's link auto-disables after 5
        consecutive failures.
        """
        assert self._client is not None
        body = await self._authed_request(link, path, params)
        if body.get("status") in (401, 100, 101, 102):
            # Withings returns these codes for "token expired".
            user_id = str(link.get("user_id") or "")
            lock = self._refresh_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                # Re-read the link row inside the locked section.
                # If a sibling caller already refreshed, the access
                # token has changed and we should retry the request
                # WITHOUT calling _refresh_token a second time.
                stale_token = str(link.get("oauth_access_token") or "")
                fresh = await self._reload_link(user_id)
                if fresh is not None:
                    fresh_token = str(fresh.get("oauth_access_token") or "")
                    if fresh_token and fresh_token != stale_token:
                        # Sibling refreshed — adopt the new token.
                        link.update(fresh)
                    else:
                        await self._refresh_token(link)
                else:
                    await self._refresh_token(link)
            body = await self._authed_request(link, path, params)
            if body.get("status") not in (0,):
                raise HealthBackendAuthError(
                    f"withings: refresh failed, status={body.get('status')}"
                )
        if body.get("status") == 601 or body.get("status") == 503:
            raise HealthBackendRateLimitError(
                "withings rate-limited",
                retry_after_seconds=60,
            )
        if body.get("status") != 0:
            raise HealthBackendTransientError(
                f"withings: status={body.get('status')} error={body.get('error')}"
            )
        return body

    async def _reload_link(self, user_id: str) -> dict[str, Any] | None:
        if self._storage is None or not user_id:
            return None
        link_id = f"{user_id}/{self.backend_name}"
        return await self._storage.get(_LINKS_COLLECTION, link_id)

    async def _authed_request(
        self,
        link: dict[str, Any],
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._client is not None
        url = f"{_API_BASE}{path}"
        access_token = str(link.get("oauth_access_token") or "")
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await self._client.post(url, data=params, headers=headers)
        except httpx.HTTPError as exc:
            raise HealthBackendTransientError(
                f"withings: HTTP error: {exc}"
            ) from exc
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After") or 60)
            raise HealthBackendRateLimitError(
                "withings 429",
                retry_after_seconds=retry_after,
            )
        try:
            return response.json()
        except Exception as exc:
            raise HealthBackendTransientError(
                f"withings: unparseable JSON: {exc}"
            ) from exc

    async def _refresh_token(self, link: dict[str, Any]) -> None:
        assert self._client is not None
        if self._storage is None:
            return
        refresh_token = str(link.get("oauth_refresh_token") or "")
        if not refresh_token:
            raise HealthBackendAuthError("withings: no refresh token")
        body = {
            "action": "requesttoken",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        try:
            response = await self._client.post(_TOKEN_URL, data=body)
            response.raise_for_status()
            json_body = response.json()
        except Exception as exc:
            raise HealthBackendAuthError(
                f"withings: refresh failed: {exc}"
            ) from exc
        if json_body.get("status") != 0:
            raise HealthBackendAuthError(
                f"withings: refresh status {json_body.get('status')}"
            )
        body_data = json_body.get("body") or {}
        link["oauth_access_token"] = str(body_data.get("access_token") or "")
        link["oauth_refresh_token"] = str(
            body_data.get("refresh_token") or refresh_token
        )
        expires_in = int(body_data.get("expires_in") or 0)
        link["oauth_expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=expires_in)
        ).isoformat()
        link_id = f"{link.get('user_id')}/{self.backend_name}"
        await self._storage.put(_LINKS_COLLECTION, link_id, link)

    def supported_metrics(self) -> set[MetricType]:
        out = {m for (m, _) in _MEASTYPE_MAP.values()}
        out.update(
            {
                MetricType.SLEEP_DURATION,
                MetricType.SLEEP_DEEP,
                MetricType.SLEEP_REM,
                MetricType.SLEEP_AWAKE,
            }
        )
        return out

