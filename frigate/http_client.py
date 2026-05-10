"""HTTP client for Frigate's REST API.

Thin wrapper around ``httpx.AsyncClient`` with two Frigate-specific
concerns: ``verify_ssl`` (LAN-only Frigate installs are commonly
self-signed) and ``http_auth_mode`` (none for unauthenticated LAN
deploys, bearer for proxy-style or Frigate API keys).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FrigateHTTP:
    def __init__(
        self,
        *,
        base_url: str,
        auth_mode: str = "none",
        token: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_mode = auth_mode
        self._token = token
        self._verify_ssl = verify_ssl
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self._verify_ssl, timeout=10.0
            )
        return self._client

    def auth_headers(self) -> dict[str, str]:
        if self._auth_mode == "bearer" and self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def get_version(self) -> str:
        if not self._base_url:
            return ""
        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._base_url}/api/version",
                headers=self.auth_headers(),
            )
        except Exception as exc:
            logger.debug("Frigate /api/version failed: %s", exc)
            return ""
        if resp.status_code != 200:
            return ""
        return resp.text.strip()

    async def get_snapshot(
        self,
        event_id: str,
        height: int = 720,
    ) -> tuple[bytes, str] | None:
        """Fetch a snapshot via Frigate's HTTP API.

        ``height`` is the requested server-side downscale (Frigate honors
        ``?h=<n>``); pass 0 for full-resolution.
        """
        if not self._base_url or not event_id:
            return None
        client = self._get_client()
        url = f"{self._base_url}/api/events/{event_id}/snapshot.jpg"
        params = {"h": height} if height else None
        try:
            resp = await client.get(
                url, headers=self.auth_headers(), params=params
            )
        except Exception as exc:
            logger.debug("Frigate snapshot fetch failed: %s", exc)
            return None
        if resp.status_code != 200:
            return None
        media = resp.headers.get("content-type", "image/jpeg")
        return resp.content, media

    def build_clip_url(self, event_id: str) -> str:
        if not self._base_url or not event_id:
            return ""
        return f"{self._base_url}/api/events/{event_id}/clip.mp4"

    async def list_cameras(self) -> list[dict[str, Any]]:
        """Return Frigate's camera config dump as a list of ``{name, ...}``.

        Frigate's ``/api/config`` returns ``{"cameras": {name: cfg}}``.
        Returns ``[]`` on any error / missing base url.
        """
        if not self._base_url:
            return []
        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._base_url}/api/config",
                headers=self.auth_headers(),
            )
        except Exception as exc:
            logger.debug("Frigate /api/config failed: %s", exc)
            return []
        if resp.status_code != 200:
            return []
        try:
            cfg = resp.json()
        except ValueError:
            return []
        cameras = cfg.get("cameras") or {}
        if not isinstance(cameras, dict):
            return []
        out: list[dict[str, Any]] = []
        for name, settings in cameras.items():
            out.append({"name": name, "settings": settings})
        return out


__all__ = ["FrigateHTTP"]
