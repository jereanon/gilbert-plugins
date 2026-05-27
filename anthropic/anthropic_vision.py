"""Anthropic Vision backend — image understanding via Claude Vision API."""

import asyncio
import base64
import logging
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.vision import VisionBackend

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


class AnthropicVision(VisionBackend):
    """Vision backend using the Anthropic Messages API with image content."""

    backend_name = "anthropic"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Anthropic API key.",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description="Vision model ID.",
                default=_DEFAULT_MODEL,
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description="Maximum tokens in vision response.",
                default=4096,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Send a tiny text-only message to the Anthropic API "
                    "to verify the API key and vision model."
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
        if not self._api_key:
            return ConfigActionResult(
                status="error",
                message="Anthropic Vision backend has no API key configured.",
            )
        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.messages.create,
                model=self._model,
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Anthropic API error: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to Anthropic Vision (model: {self._model}).",
        )

    def __init__(self) -> None:
        self._api_key: str = ""
        self._model: str = _DEFAULT_MODEL
        self._max_tokens: int = 4096
        self._client: Any = None
        # Latched on the first 401 we see. Causes ``available`` to flip
        # to False so the PDF indexing loop (which checks per page) stops
        # attempting vision for the rest of the session. Cleared on
        # ``initialize()`` so editing the API key in settings → restart
        # retries automatically.
        self._auth_failed: bool = False

    async def initialize(self, config: dict[str, Any]) -> None:
        from .shared_key import (
            get_shared_anthropic_api_key,
            register_anthropic_api_key,
        )

        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_MODEL))
        self._max_tokens = int(config.get("max_tokens", 4096))
        self._auth_failed = False
        self._client = None

        # Plugin-local key sharing: if vision was started without its
        # own API key but the AI / OCR backend already has one from
        # the same plugin, reuse it. Saves the operator from pasting
        # the same Anthropic key into Settings → Vision when they've
        # already set it under Settings → AI. The per-backend value
        # always wins when both are set.
        if self._api_key:
            register_anthropic_api_key(self._api_key, source="vision")
            logger.info(
                "Anthropic Vision backend initialized (model=%s)",
                self._model,
            )
        else:
            shared = get_shared_anthropic_api_key()
            if shared:
                self._api_key = shared
                logger.info(
                    "Anthropic Vision backend initialized (model=%s, "
                    "api_key sourced from a sibling Anthropic backend)",
                    self._model,
                )
            else:
                logger.warning(
                    "Anthropic Vision backend: no API key configured "
                    "(no sibling Anthropic backend had one to share)"
                )

    async def close(self) -> None:
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key) and not self._auth_failed

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        if not self._api_key or self._auth_failed:
            return ""

        try:
            client = self._get_client()
            b64_data = base64.standard_b64encode(image_bytes).decode("ascii")

            response = await asyncio.to_thread(
                client.messages.create,
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Extract ALL technical content from this page image as plain structured text. "
                                    "Include: pinout tables, wiring diagrams, connector assignments, component "
                                    "specifications, part numbers, voltage/current ratings, communication protocols, "
                                    "dimensions, torque specs, and any other technical data. Reproduce tables as "
                                    "aligned text columns. Label diagram elements clearly (e.g., 'Pin 1: CAN_H, "
                                    "Pin 2: CAN_L'). Do NOT describe the visual layout — extract the information "
                                    "content only. If the page contains no technical content, respond with an "
                                    "empty string."
                                ),
                            },
                        ],
                    }
                ],
            )

            text_parts: list[str] = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)

            return "\n".join(text_parts).strip()

        except Exception as exc:
            if self._is_auth_error(exc):
                # Latch the failure so the indexing loop stops calling us
                # per-page. Log once, loud and clear, so the user can see
                # that vision is off until they fix the key.
                self._auth_failed = True
                logger.warning(
                    "Anthropic Vision: API key rejected (401). Disabling "
                    "vision extraction for this session — fix the key in "
                    "Settings → Intelligence → Vision and the backend will "
                    "retry automatically on restart.",
                )
                return ""
            logger.warning("Vision describe_image failed", exc_info=True)
            return ""

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        """Detect Anthropic authentication failures across SDK versions."""
        # Prefer the SDK's typed exception when available
        try:
            import anthropic

            if isinstance(exc, anthropic.AuthenticationError):
                return True
        except Exception:
            pass
        # Fall back to status-code / message heuristics
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status == 401:
            return True
        message = str(exc).lower()
        return "invalid x-api-key" in message or "authentication_error" in message
