"""Google Workspace Directory integration — user/group provider.

Self-contained — builds its own Google Admin SDK client from a pasted
service account JSON key. Acts as a UserProviderBackend so the
UserService can automatically sync external users into the local store.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.users import ExternalUser, UserProviderBackend

from ._google_retry import call_with_retry

logger = logging.getLogger(__name__)


class GoogleDirectoryBackend(UserProviderBackend):
    """Provides users and groups from Google Workspace Admin Directory."""

    backend_name = "google_directory"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="sa_json",
                type=ToolParameterType.STRING,
                description="Google service account key (paste JSON content).",
                sensitive=True,
                restart_required=True,
                multiline=True,
            ),
            ConfigParam(
                key="delegated_user",
                type=ToolParameterType.STRING,
                description="Admin email to impersonate for directory API.",
                restart_required=True,
            ),
            ConfigParam(
                key="domain",
                type=ToolParameterType.STRING,
                description="Google Workspace domain to sync users from.",
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "List a single user from the Google Workspace domain "
                    "as a smoke test of the service account credentials."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._directory is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "Google Directory is not initialized — check "
                    "sa_json, delegated_user, and domain, then save and "
                    "restart."
                ),
            )
        try:
            response = await self._call(
                lambda svc: svc.users().list(domain=self._domain, maxResults=1)
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Google Directory API error: {exc}",
            )
        users = response.get("users", [])
        return ConfigActionResult(
            status="ok",
            message=(
                f"Connected to Google Directory (domain={self._domain}). "
                f"Smoke test returned {len(users)} user(s)."
            ),
        )

    def __init__(self) -> None:
        self._domain: str = ""
        self._directory: Any = None
        self._creds: Any = None  # cached so we can rebuild after stale-socket errors
        self._cached_users: list[ExternalUser] | None = None
        self._cached_groups: list[dict[str, Any]] | None = None

    async def initialize(self, config: dict[str, Any]) -> None:
        sa_json = config.get("sa_json", "")
        delegated_user = config.get("delegated_user", "")
        self._domain = config.get("domain", "")

        if not sa_json:
            logger.warning("Google Directory: no sa_json configured")
            return

        try:
            sa_info = json.loads(sa_json) if isinstance(sa_json, str) else sa_json

            from google.oauth2 import service_account

            scopes = [
                "https://www.googleapis.com/auth/admin.directory.user.readonly",
                "https://www.googleapis.com/auth/admin.directory.group.readonly",
            ]
            creds = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=scopes,
            )
            if delegated_user:
                creds = creds.with_subject(delegated_user)
            self._creds = creds

            self._directory = await asyncio.to_thread(self._build_service)
            logger.info("Google Directory backend initialized (domain=%s)", self._domain or "(any)")
        except Exception:
            logger.exception("Failed to initialize Google Directory backend")

    async def close(self) -> None:
        self._directory = None
        self._creds = None
        self._cached_users = None
        self._cached_groups = None

    def _build_service(self) -> Any:
        from googleapiclient.discovery import build

        return build("admin", "directory_v1", credentials=self._creds)

    def _ensure_service(self) -> Any:
        if self._directory is None:
            raise RuntimeError("Google Directory backend not initialized")
        return self._directory

    async def _rebuild_service(self) -> None:
        if self._creds is None:
            raise RuntimeError(
                "Google Directory backend has no cached credentials to rebuild the service"
            )
        self._directory = await asyncio.to_thread(self._build_service)

    async def _call(self, build_call: Callable[[Any], Any]) -> Any:
        """Run a Directory API call with one-shot retry on stale connections."""
        return await call_with_retry(
            get_service=self._ensure_service,
            rebuild=self._rebuild_service,
            build_call=build_call,
            name="Directory",
        )

    # --- UserProviderBackend ---

    @property
    def provider_type(self) -> str:
        return "google"

    async def list_external_users(self) -> list[ExternalUser]:
        if self._cached_users is not None:
            return self._cached_users

        try:
            users = await self._fetch_users()
            groups = await self._fetch_groups_with_members()
            self._assign_groups_to_users(users, groups)
            self._cached_users = users
            logger.info("Fetched %d users from Google Directory", len(users))
            return users
        except Exception:
            logger.exception("Failed to fetch users from Google Directory")
            return []

    async def get_external_user(self, provider_user_id: str) -> ExternalUser | None:
        users = await self.list_external_users()
        for user in users:
            if user.provider_user_id == provider_user_id:
                return user
        return None

    async def get_external_user_by_email(self, email: str) -> ExternalUser | None:
        users = await self.list_external_users()
        for user in users:
            if user.email == email:
                return user
        return None

    async def list_groups(self) -> list[dict[str, Any]]:
        if self._cached_groups is not None:
            return self._cached_groups

        try:
            groups = await self._fetch_groups_with_members()
            self._cached_groups = groups
            return groups
        except Exception:
            logger.exception("Failed to fetch groups from Google Directory")
            return []

    def invalidate_cache(self) -> None:
        self._cached_users = None
        self._cached_groups = None

    # --- Internal ---

    async def _fetch_users(self) -> list[ExternalUser]:
        if self._directory is None:
            return []

        users: list[ExternalUser] = []
        page_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "domain": self._domain,
                "maxResults": 500,
                "orderBy": "email",
                "projection": "full",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = await self._call(
                lambda svc, kw=kwargs: svc.users().list(**kw)
            )

            for u in response.get("users", []):
                if u.get("suspended", False):
                    continue
                email = u.get("primaryEmail", "")
                if not email:
                    continue

                name = u.get("name", {})
                roles: list[str] = []
                if u.get("isAdmin", False):
                    roles.append("admin")

                users.append(
                    ExternalUser(
                        provider_type="google",
                        provider_user_id=u.get("id", ""),
                        email=email,
                        display_name=name.get("fullName", email.split("@")[0]),
                        roles=roles,
                        metadata={
                            "google_id": u.get("id", ""),
                            "org_unit_path": u.get("orgUnitPath", "/"),
                            "is_admin": u.get("isAdmin", False),
                            "is_delegated_admin": u.get("isDelegatedAdmin", False),
                            "creation_time": u.get("creationTime", ""),
                            "last_login_time": u.get("lastLoginTime", ""),
                            "thumbnail_photo_url": u.get("thumbnailPhotoUrl", ""),
                            "aliases": u.get("aliases", []),
                            "non_editable_aliases": u.get("nonEditableAliases", []),
                            "phones": [
                                {"value": p.get("value", ""), "type": p.get("type", "")}
                                for p in u.get("phones", [])
                            ],
                            "addresses": [
                                {"type": a.get("type", ""), "formatted": a.get("formatted", "")}
                                for a in u.get("addresses", [])
                            ],
                            "organizations": [
                                {
                                    "title": o.get("title", ""),
                                    "department": o.get("department", ""),
                                    "name": o.get("name", ""),
                                    "primary": o.get("primary", False),
                                }
                                for o in u.get("organizations", [])
                            ],
                            "recovery_email": u.get("recoveryEmail", ""),
                            "recovery_phone": u.get("recoveryPhone", ""),
                        },
                    )
                )

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return users

    async def _fetch_groups_with_members(self) -> list[dict[str, Any]]:
        if self._directory is None:
            return []

        groups: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {"domain": self._domain, "maxResults": 200}
            if page_token:
                kwargs["pageToken"] = page_token
            response = await self._call(
                lambda svc, kw=kwargs: svc.groups().list(**kw)
            )

            for g in response.get("groups", []):
                group_info: dict[str, Any] = {
                    "id": g.get("id", ""),
                    "email": g.get("email", ""),
                    "name": g.get("name", ""),
                    "description": g.get("description", ""),
                    "members": [],
                }

                try:
                    mem_page_token: str | None = None
                    while True:
                        mem_kwargs: dict[str, Any] = {
                            "groupKey": g.get("email", ""),
                            "maxResults": 500,
                        }
                        if mem_page_token:
                            mem_kwargs["pageToken"] = mem_page_token
                        mem_resp = await self._call(
                            lambda svc, kw=mem_kwargs: svc.members().list(**kw)
                        )
                        for m in mem_resp.get("members", []):
                            group_info["members"].append(m.get("email", ""))
                        mem_page_token = mem_resp.get("nextPageToken")
                        if not mem_page_token:
                            break
                except Exception:
                    logger.debug("Could not list members for group %s", g.get("email"))

                groups.append(group_info)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return groups

    @staticmethod
    def _assign_groups_to_users(users: list[ExternalUser], groups: list[dict[str, Any]]) -> None:
        email_to_user = {u.email: u for u in users}
        for group in groups:
            group_name = group.get("name", group.get("email", ""))
            for member_email in group.get("members", []):
                user = email_to_user.get(member_email)
                if user is not None:
                    user.groups.append(group_name)
