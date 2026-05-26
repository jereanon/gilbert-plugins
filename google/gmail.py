"""Gmail email backend — self-contained EmailBackend using Gmail API v1.

Authenticates directly with a Google service account (JSON key pasted
into config) and domain-wide delegation. No shared GoogleService needed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.email import (
    EmailAddress,
    EmailAttachment,
    EmailBackend,
    EmailMessage,
    TransientEmailError,
)
from gilbert.interfaces.tools import ToolParameterType

from ._google_retry import (
    TRANSIENT_TRANSPORT_EXCS,
    call_with_retry,
    is_transient_http_error,
)
from .google_credentials import (
    GoogleCredentialMode,
    build_google_credentials,
    build_google_oauth_authorization_url,
    exchange_google_oauth_code,
    google_credential_spec_from_config,
    require_google_credential_mode,
)

logger = logging.getLogger(__name__)


class GmailBackend(EmailBackend):
    """EmailBackend backed by Gmail API v1 via google-api-python-client."""

    backend_name = "gmail"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="credential_mode",
                type=ToolParameterType.STRING,
                description="Google credential mode. Use oauth_bot for ordinary Gmail accounts.",
                default=GoogleCredentialMode.OAUTH_BOT.value,
                choices=(
                    GoogleCredentialMode.OAUTH_BOT.value,
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT.value,
                    GoogleCredentialMode.SHARED_SERVICE_ACCOUNT.value,
                ),
                restart_required=True,
            ),
            ConfigParam(
                key="email_address",
                type=ToolParameterType.STRING,
                description="Email address (mailbox to monitor and send from).",
                restart_required=True,
            ),
            ConfigParam(
                key="service_account_json",
                type=ToolParameterType.STRING,
                description="Google service account key (paste JSON content).",
                sensitive=True,
                restart_required=True,
                multiline=True,
            ),
            ConfigParam(
                key="delegated_user",
                type=ToolParameterType.STRING,
                description="Email of the user to impersonate via domain-wide delegation.",
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
                description="Open Google's OAuth consent screen for this Gmail backend.",
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
                    "Fetch the Gmail profile to verify credentials."
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
                    "Gmail backend is not initialized — check "
                    "service_account_json and delegated_user, then save "
                    "and restart."
                ),
            )
        try:
            profile = await self._call(lambda svc: svc.users().getProfile(userId="me"))
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Gmail API error: {exc}",
            )
        email = profile.get("emailAddress", "(unknown)")
        total = profile.get("messagesTotal", 0)
        return ConfigActionResult(
            status="ok",
            message=f"Connected to Gmail as {email} ({total} messages).",
        )

    def _action_connect_google(self, payload: dict[str, Any]) -> ConfigActionResult:
        cfg = self._payload_config(payload)
        scopes = self._scopes()
        client_id = str(cfg.get("oauth_client_id") or "")
        redirect_uri = str(cfg.get("oauth_redirect_uri") or "urn:ietf:wg:oauth:2.0:oob")
        if not client_id:
            return ConfigActionResult(
                status="error",
                message="oauth_client_id is required before connecting Google.",
            )
        url = build_google_oauth_authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
        return ConfigActionResult(
            status="pending",
            message="Open Google, approve access, paste the code into oauth_auth_code, then continue.",
            open_url=url,
            followup_action="connect_google_complete",
        )

    async def _action_connect_google_complete(self, payload: dict[str, Any]) -> ConfigActionResult:
        cfg = self._payload_config(payload)
        auth_code = str(cfg.get("oauth_auth_code") or "")
        if not auth_code:
            return ConfigActionResult(
                status="error",
                message="Paste the Google authorization code into oauth_auth_code first.",
            )
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
        self._service: Any = None  # gmail API resource
        self._creds: Any = None  # cached credentials so we can rebuild _service

    @staticmethod
    def _scopes() -> tuple[str, ...]:
        return (
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        )

    @staticmethod
    def _payload_config(payload: dict[str, Any]) -> dict[str, Any]:
        cfg = payload.get("config") if isinstance(payload, dict) else None
        return dict(cfg if isinstance(cfg, dict) else payload)

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        if config is None:
            return

        self._email_address = config.get("email_address", "")

        try:
            spec = google_credential_spec_from_config(
                config,
                scopes=self._scopes(),
                legacy_delegated_user=self._email_address,
            )
            require_google_credential_mode(
                spec,
                supported_modes={
                    GoogleCredentialMode.OAUTH_BOT,
                    GoogleCredentialMode.DELEGATED_SERVICE_ACCOUNT,
                },
                backend_label="Gmail",
            )
            self._creds = build_google_credentials(spec)

            self._service = await asyncio.to_thread(self._build_service)
            logger.info("Gmail backend initialized (email=%s)", self._email_address)
        except Exception:
            logger.exception("Failed to initialize Gmail backend")

    async def close(self) -> None:
        self._service = None
        self._creds = None

    def _ensure_service(self) -> Any:
        if self._service is None:
            raise RuntimeError("Gmail backend not initialized — check service_account_json config")
        return self._service

    def _build_service(self) -> Any:
        """Construct a fresh Gmail API service from cached creds.

        Used both at initialize time and to recover from stale-connection
        errors. Runs synchronously — call via ``asyncio.to_thread``.
        """
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=self._creds)

    async def _rebuild_service(self) -> None:
        """Replace ``self._service`` after a transport error so a follow-up
        call gets a fresh ``httplib2.Http`` (and therefore a fresh TLS socket).
        Tests can patch this to install a new fake service.
        """
        if self._creds is None:
            raise RuntimeError(
                "Gmail backend has no cached credentials to rebuild the service"
            )
        self._service = await asyncio.to_thread(self._build_service)

    async def _call(self, build_call: Callable[[Any], Any]) -> Any:
        """Run a Gmail API call with one-shot retry on stale connections,
        translating still-failing transport errors and transient HTTP
        responses (429/5xx) into ``TransientEmailError`` so the outbox
        flusher can back off and re-queue.
        """
        try:
            return await call_with_retry(
                get_service=self._ensure_service,
                rebuild=self._rebuild_service,
                build_call=build_call,
                name="Gmail",
            )
        except TRANSIENT_TRANSPORT_EXCS as exc:
            raise TransientEmailError(
                f"Gmail transport failure after rebuild + retry: {exc}"
            ) from exc
        except Exception as exc:
            if is_transient_http_error(exc):
                raise TransientEmailError(str(exc)) from exc
            raise

    # --- Fetch ---

    async def list_message_ids(self, query: str = "", max_results: int = 100) -> list[str]:
        q = query or "in:inbox OR in:sent"

        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < max_results:
            params: dict[str, Any] = {
                "userId": "me",
                "q": q,
                "maxResults": min(100, max_results - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token

            result = await self._call(
                lambda svc, params=params: svc.users().messages().list(**params)
            )
            for m in result.get("messages", []):
                ids.append(m["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return ids

    async def get_message(self, message_id: str) -> EmailMessage | None:
        try:
            data = await self._call(
                lambda svc: svc.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
            )
        except TransientEmailError:
            raise
        except Exception:
            logger.warning("Failed to fetch message %s", message_id, exc_info=True)
            return None

        headers = {
            h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])
        }

        sender = _parse_sender(headers.get("from", ""))
        to = _parse_address_list(headers.get("to", ""))
        cc = _parse_address_list(headers.get("cc", ""))
        date = _parse_date(headers.get("date", ""))
        body_text, body_html = _extract_body(data.get("payload", {}))

        return EmailMessage(
            message_id=data["id"],
            thread_id=data.get("threadId", ""),
            subject=headers.get("subject", "(no subject)"),
            sender=sender,
            to=to,
            cc=cc,
            body_text=body_text,
            body_html=body_html,
            date=date,
            in_reply_to=headers.get("message-id", ""),
            headers=headers,
        )

    # --- Send ---

    async def send(
        self,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        in_reply_to: str = "",
        thread_id: str = "",
        attachments: list[EmailAttachment] | None = None,
        reply_to: EmailAddress | None = None,
        from_name: str = "",
    ) -> str:
        self._ensure_service()

        if attachments:
            msg = MIMEMultipart("mixed")
            body_part = MIMEMultipart("alternative")
            if body_text:
                body_part.attach(MIMEText(body_text, "plain"))
            body_part.attach(MIMEText(body_html, "html"))
            msg.attach(body_part)

            for att in attachments:
                from email.mime.application import MIMEApplication

                part = MIMEApplication(att.data, Name=att.filename)
                part["Content-Disposition"] = f'attachment; filename="{att.filename}"'
                if att.mime_type:
                    part.set_type(att.mime_type)
                msg.attach(part)
        else:
            msg = MIMEMultipart("alternative")
            if body_text:
                msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))

        msg["To"] = ", ".join(str(a) for a in to)
        msg["Subject"] = subject
        if from_name:
            msg["From"] = str(EmailAddress(email=self._email_address, name=from_name))
        else:
            msg["From"] = self._email_address
        if reply_to:
            msg["Reply-To"] = str(reply_to)
        if cc:
            msg["Cc"] = ", ".join(str(a) for a in cc)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
            if not subject.startswith("Re:"):
                msg["Subject"] = f"Re: {subject}"

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        send_body: dict[str, str] = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        result = await self._call(
            lambda svc: svc.users().messages().send(userId="me", body=send_body)
        )
        return result.get("id", "")

    # --- Mark ---

    async def mark_read(self, message_id: str) -> None:
        await self._call(
            lambda svc: svc.users()
            .messages()
            .modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]})
        )


# --- Helpers ---


def _parse_sender(from_header: str) -> EmailAddress:
    """Parse a From header into an EmailAddress."""
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        return EmailAddress(email=match.group(2).strip(), name=match.group(1).strip())
    return EmailAddress(email=from_header.strip().strip("<>"))


def _parse_address_list(header: str) -> list[EmailAddress]:
    """Parse a To/CC header into a list of EmailAddress."""
    if not header or not header.strip():
        return []

    addresses: list[EmailAddress] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', part.strip())
        if match:
            addresses.append(
                EmailAddress(
                    email=match.group(2).strip().lower(),
                    name=match.group(1).strip(),
                )
            )
        elif "@" in part:
            addresses.append(EmailAddress(email=part.strip().lower()))
    return addresses


def _parse_date(date_str: str) -> datetime:
    """Best-effort parse of email Date header."""
    if not date_str:
        return datetime.now(UTC)
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.now(UTC)


def _extract_body(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract (plain_text, html) from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
        return text, ""

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
        stripped = re.sub(r"<[^>]+>", "", html)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        return stripped, html

    # Multipart
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")

        if part_mime == "text/plain" and not plain_text:
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        elif part_mime == "text/html" and not html_text:
            data = part.get("body", {}).get("data", "")
            if data:
                html_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        elif part_mime.startswith("multipart/"):
            nested_plain, nested_html = _extract_body(part)
            if nested_plain and not plain_text:
                plain_text = nested_plain
            if nested_html and not html_text:
                html_text = nested_html

    if not plain_text and html_text:
        plain_text = re.sub(r"<[^>]+>", "", html_text)
        plain_text = re.sub(r"\s+", " ", plain_text).strip()

    return plain_text, html_text
