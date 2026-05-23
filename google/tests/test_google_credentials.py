from __future__ import annotations

import json
from typing import Any

import pytest
from gilbert_plugin_google.google_credentials import (
    GoogleCredentialMode,
    GoogleCredentialSpec,
    UnsupportedGoogleCredentialMode,
    build_google_credentials,
    build_google_oauth_authorization_url,
    exchange_google_oauth_code,
    google_credential_spec_from_config,
    require_google_credential_mode,
    service_account_email,
)

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
)


def test_oauth_bot_builds_refreshable_user_credentials() -> None:
    spec = GoogleCredentialSpec(
        mode=GoogleCredentialMode.OAUTH_BOT,
        scopes=SCOPES,
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="refresh-token",
    )

    creds = build_google_credentials(spec)

    assert creds.refresh_token == "refresh-token"
    assert creds.client_id == "client-id"
    assert creds.client_secret == "client-secret"
    assert creds.scopes == list(SCOPES)


def test_delegated_service_account_applies_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeCredentials:
        def with_subject(self, delegated_user: str) -> FakeCredentials:
            calls["delegated_user"] = delegated_user
            return self

    def from_service_account_info(info: dict[str, Any], scopes: tuple[str, ...]) -> FakeCredentials:
        calls["info"] = info
        calls["scopes"] = scopes
        return FakeCredentials()

    import gilbert_plugin_google.google_credentials as google_credentials

    credentials_cls = google_credentials._import_google_attr(  # noqa: SLF001
        "google.oauth2.service_account",
        "Credentials",
    )

    monkeypatch.setattr(
        credentials_cls,
        "from_service_account_info",
        staticmethod(from_service_account_info),
    )

    creds = build_google_credentials(
        GoogleCredentialSpec(
            mode=GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT,
            scopes=SCOPES,
            service_account_json=json.dumps({"client_email": "sa@example.iam.gserviceaccount.com"}),
            delegated_user="alice@example.com",
        )
    )

    assert isinstance(creds, FakeCredentials)
    assert calls["info"]["client_email"] == "sa@example.iam.gserviceaccount.com"
    assert calls["scopes"] == SCOPES
    assert calls["delegated_user"] == "alice@example.com"


def test_shared_service_account_does_not_apply_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {"with_subject": 0}

    class FakeCredentials:
        def with_subject(self, delegated_user: str) -> FakeCredentials:
            calls["with_subject"] += 1
            return self

    def from_service_account_info(info: dict[str, Any], scopes: tuple[str, ...]) -> FakeCredentials:
        calls["info"] = info
        calls["scopes"] = scopes
        return FakeCredentials()

    import gilbert_plugin_google.google_credentials as google_credentials

    credentials_cls = google_credentials._import_google_attr(  # noqa: SLF001
        "google.oauth2.service_account",
        "Credentials",
    )

    monkeypatch.setattr(
        credentials_cls,
        "from_service_account_info",
        staticmethod(from_service_account_info),
    )

    build_google_credentials(
        GoogleCredentialSpec(
            mode=GoogleCredentialMode.SHARED_SERVICE_ACCOUNT,
            scopes=SCOPES,
            service_account_json={"client_email": "share-me@example.iam.gserviceaccount.com"},
        )
    )

    assert calls["with_subject"] == 0
    assert calls["scopes"] == SCOPES


def test_shared_mode_rejected_with_actionable_message() -> None:
    spec = GoogleCredentialSpec(
        mode=GoogleCredentialMode.SHARED_SERVICE_ACCOUNT,
        scopes=SCOPES,
        service_account_json={},
    )

    with pytest.raises(UnsupportedGoogleCredentialMode) as exc:
        require_google_credential_mode(
            spec,
            supported_modes={
                GoogleCredentialMode.OAUTH_BOT,
                GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT,
            },
            backend_label="Gmail",
        )

    assert "Gmail does not support credential_mode=shared_service_account" in str(exc.value)
    assert "Use credential_mode=oauth_bot for personal Google accounts" in str(exc.value)


def test_spec_from_config_defaults_to_oauth_for_new_setup() -> None:
    spec = google_credential_spec_from_config({}, scopes=SCOPES)

    assert spec.mode is GoogleCredentialMode.OAUTH_BOT


def test_spec_from_config_preserves_legacy_delegated_service_account() -> None:
    spec = google_credential_spec_from_config(
        {
            "service_account_json": "{}",
            "delegated_user": "alice@example.com",
        },
        scopes=SCOPES,
    )

    assert spec.mode is GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT
    assert spec.delegated_user == "alice@example.com"


def test_oauth_authorization_url_contains_least_privilege_scopes() -> None:
    url = build_google_oauth_authorization_url(
        client_id="client-id",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes=SCOPES,
        state="state-token",
    )

    assert "client_id=client-id" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=state-token" in url
    assert "gmail.modify" in url
    assert "gmail.send" in url


@pytest.mark.asyncio
async def test_exchange_oauth_code_returns_persist_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"refresh_token": "new-refresh-token", "access_token": "access-token"}

    captured: dict[str, Any] = {}

    async def fake_post(url: str, data: dict[str, Any]) -> FakeResponse:
        captured["url"] = url
        captured["data"] = data
        return FakeResponse()

    import gilbert_plugin_google.google_credentials as google_credentials

    monkeypatch.setattr(google_credentials.httpx, "post", fake_post)

    persist = await exchange_google_oauth_code(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        auth_code="auth-code",
    )

    assert captured["data"]["code"] == "auth-code"
    assert persist == {
        "oauth_refresh_token": "new-refresh-token",
        "oauth_auth_code": "",
    }


def test_service_account_email_extracts_share_target() -> None:
    assert (
        service_account_email({"client_email": "share-me@example.iam.gserviceaccount.com"})
        == "share-me@example.iam.gserviceaccount.com"
    )
