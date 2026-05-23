"""Google credential helpers shared by the google std-plugin backends."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
_PLUGIN_DIR = Path(__file__).resolve().parent


class GoogleCredentialMode(StrEnum):
    OAUTH_BOT = "oauth_bot"
    DELEGATED_SERVICE_ACCOUNT = "delegated_service_account"
    SHARED_SERVICE_ACCOUNT = "shared_service_account"


class UnsupportedGoogleCredentialModeError(ValueError):
    """Raised when a backend receives a valid mode it cannot use."""


UnsupportedGoogleCredentialMode = UnsupportedGoogleCredentialModeError


@dataclass(frozen=True)
class GoogleCredentialSpec:
    mode: GoogleCredentialMode
    scopes: tuple[str, ...]
    service_account_json: str | dict[str, Any] = ""
    delegated_user: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_uri: str = OOB_REDIRECT_URI
    oauth_refresh_token: str = ""
    oauth_auth_code: str = ""


def _parse_service_account_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        raise ValueError("service_account_json is required for service-account credential modes")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid service_account_json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("service_account_json must decode to a JSON object")
    return parsed


def service_account_email(raw: str | dict[str, Any]) -> str:
    """Return the service-account email users can share Calendar/Drive with."""

    info = _parse_service_account_json(raw)
    return str(info.get("client_email") or "")


def google_credential_spec_from_config(
    config: dict[str, Any],
    *,
    scopes: tuple[str, ...],
    default_mode: GoogleCredentialMode = GoogleCredentialMode.OAUTH_BOT,
    legacy_delegated_user: str = "",
) -> GoogleCredentialSpec:
    """Build a credential spec from backend config.

    ``credential_mode`` is explicit for new configs. If it is absent,
    legacy service-account configs with a delegated user keep using
    domain-wide delegation. Otherwise new setup defaults to OAuth.
    """

    mode_raw = str(config.get("credential_mode") or "")
    delegated_user = str(config.get("delegated_user") or legacy_delegated_user or "")
    service_account_json = config.get("service_account_json", "")
    if mode_raw:
        mode = GoogleCredentialMode(mode_raw)
    elif service_account_json and delegated_user:
        mode = GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT
    elif service_account_json and not delegated_user:
        mode = GoogleCredentialMode.SHARED_SERVICE_ACCOUNT
    else:
        mode = default_mode

    return GoogleCredentialSpec(
        mode=mode,
        scopes=scopes,
        service_account_json=service_account_json,
        delegated_user=delegated_user,
        oauth_client_id=str(config.get("oauth_client_id") or ""),
        oauth_client_secret=str(config.get("oauth_client_secret") or ""),
        oauth_redirect_uri=str(config.get("oauth_redirect_uri") or OOB_REDIRECT_URI),
        oauth_refresh_token=str(config.get("oauth_refresh_token") or ""),
        oauth_auth_code=str(config.get("oauth_auth_code") or ""),
    )


def require_google_credential_mode(
    spec: GoogleCredentialSpec,
    *,
    supported_modes: set[GoogleCredentialMode],
    backend_label: str,
) -> None:
    if spec.mode in supported_modes:
        return
    supported = ", ".join(sorted(m.value for m in supported_modes))
    if spec.mode is GoogleCredentialMode.SHARED_SERVICE_ACCOUNT:
        hint = (
            "Use credential_mode=oauth_bot for personal Google accounts, or "
            "credential_mode=delegated_service_account for Workspace domain-wide delegation."
        )
    else:
        hint = f"Supported modes for {backend_label}: {supported}."
    raise UnsupportedGoogleCredentialModeError(
        f"{backend_label} does not support credential_mode={spec.mode.value}. {hint}"
    )


def build_google_credentials(spec: GoogleCredentialSpec) -> Any:
    if spec.mode is GoogleCredentialMode.OAUTH_BOT:
        if not spec.oauth_client_id or not spec.oauth_client_secret:
            raise ValueError("oauth_client_id and oauth_client_secret are required for oauth_bot")
        if not spec.oauth_refresh_token:
            raise ValueError(
                "oauth_refresh_token is required for oauth_bot. Run connect_google first."
            )
        credentials_cls = _import_google_attr("google.oauth2.credentials", "Credentials")

        return credentials_cls(
            token=None,
            refresh_token=spec.oauth_refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=spec.oauth_client_id,
            client_secret=spec.oauth_client_secret,
            scopes=list(spec.scopes),
        )

    info = _parse_service_account_json(spec.service_account_json)
    credentials_cls = _import_google_attr("google.oauth2.service_account", "Credentials")
    creds = credentials_cls.from_service_account_info(
        info,
        scopes=spec.scopes,
    )
    if spec.mode is GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT:
        if not spec.delegated_user:
            raise ValueError(
                "delegated_user is required for delegated_service_account"
            )
        creds = creds.with_subject(spec.delegated_user)
    return creds


def _import_google_attr(module_name: str, attr: str) -> Any:
    """Import from Google's namespace package even under pytest's plugin path.

    Pytest can import this plugin's ``google`` directory as top-level
    ``google`` while collecting tests. Runtime plugin loading does not do
    that, but this guard keeps helper tests and ad-hoc imports reliable.
    """

    google_pkg = sys.modules.get("google")
    google_file = Path(str(getattr(google_pkg, "__file__", "") or ""))
    if google_file and _PLUGIN_DIR in google_file.parents:
        for name in list(sys.modules):
            if name == "google" or name.startswith("google."):
                del sys.modules[name]
    module = import_module(module_name)
    return getattr(module, attr)


def build_google_oauth_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str = "",
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URI}?{urlencode(params)}"


async def exchange_google_oauth_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    auth_code: str,
) -> dict[str, str]:
    response = await asyncio.to_thread(
        httpx.post,
        GOOGLE_TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": auth_code,
            "grant_type": "authorization_code",
        },
    )
    response.raise_for_status()
    data = response.json()
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token:
        raise ValueError(
            "Google did not return a refresh token. Re-run connect_google and approve offline access."
        )
    return {
        "oauth_refresh_token": refresh_token,
        "oauth_auth_code": "",
    }
