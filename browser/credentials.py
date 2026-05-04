"""Encrypted-at-rest browser credential store.

One row per ``(user_id, site, username)`` in the
``browser_credentials`` collection. ``username`` and ``password`` are
sealed with a Fernet key kept at ``<plugin_data>/fernet.key`` (mode
600). The key is generated on first start if absent. See verification
finding 0.5 for the threat model and rationale.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from gilbert.interfaces.storage import Filter, FilterOp, Query

logger = logging.getLogger(__name__)


COLLECTION = "browser_credentials"


@dataclass
class BrowserCredential:
    user_id: str
    site: str
    label: str
    username: str
    password: str
    login_url: str = ""
    id: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""


class CredentialStore:
    """Per-installation Fernet-sealed credential store.

    The store keeps a single symmetric key on disk. Loss of that key
    renders all stored credentials unrecoverable; back it up alongside
    the rest of ``.gilbert/`` or re-enter passwords in the UI.
    """

    def __init__(self, storage: Any, key_path: Path) -> None:
        self._storage = storage
        self._key_path = key_path
        self._fernet: Fernet | None = None

    async def start(self) -> None:
        if self._key_path.exists():
            key = self._key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic, mode-600 write.
            fd = os.open(
                str(self._key_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        self._fernet = Fernet(key)

    async def save(self, cred: BrowserCredential) -> BrowserCredential:
        assert self._fernet is not None
        cred_id = cred.id or secrets.token_urlsafe(12)
        row = {
            "_id": cred_id,
            "user_id": cred.user_id,
            "site": cred.site,
            "label": cred.label,
            "login_url": cred.login_url,
            "username_selector": cred.username_selector,
            "password_selector": cred.password_selector,
            "submit_selector": cred.submit_selector,
            "username_enc": self._fernet.encrypt(cred.username.encode()).decode(),
            "password_enc": self._fernet.encrypt(cred.password.encode()).decode(),
        }
        await self._storage.put(COLLECTION, cred_id, row)
        cred.id = cred_id
        return cred

    async def get(self, cred_id: str, user_id: str) -> BrowserCredential:
        row = await self._storage.get(COLLECTION, cred_id)
        if row is None:
            raise KeyError(cred_id)
        if row.get("user_id") != user_id:
            raise PermissionError("not your credential")
        return self._row_to_cred(row, include_password=True)

    async def list_for_user(self, user_id: str) -> list[BrowserCredential]:
        query = Query(
            collection=COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
        )
        rows = await self._storage.query(query)
        # Lists never include passwords — callers display username + label
        # only. The per-id ``get`` is the only path that decrypts the
        # password, and it's never sent to the UI.
        return [self._row_to_cred(r, include_password=False) for r in rows]

    async def delete(self, cred_id: str, user_id: str) -> None:
        # Auth check via get(); raises if cred doesn't belong to user.
        await self.get(cred_id, user_id)
        await self._storage.delete(COLLECTION, cred_id)

    def _row_to_cred(
        self, row: dict[str, Any], *, include_password: bool
    ) -> BrowserCredential:
        assert self._fernet is not None
        try:
            username = self._fernet.decrypt(row["username_enc"]).decode()
        except (InvalidToken, KeyError):
            logger.warning("could not decrypt username for credential %s", row.get("_id"))
            username = ""
        password = ""
        if include_password:
            try:
                password = self._fernet.decrypt(row["password_enc"]).decode()
            except (InvalidToken, KeyError):
                logger.warning(
                    "could not decrypt password for credential %s", row.get("_id")
                )
        return BrowserCredential(
            id=row.get("_id", ""),
            user_id=row.get("user_id", ""),
            site=row.get("site", ""),
            label=row.get("label", ""),
            username=username,
            password=password,
            login_url=row.get("login_url", ""),
            username_selector=row.get("username_selector", ""),
            password_selector=row.get("password_selector", ""),
            submit_selector=row.get("submit_selector", ""),
        )
