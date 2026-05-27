"""Tests for the look_at_what_im_seeing AI tool.

Drives ``execute_camera_tool`` directly with stub session +
vision/ocr backends to verify:
- focus="text" routes to OCR, returns extracted text
- focus="general" / "face" routes to vision, returns description
- camera-less device returns a friendly error
- missing vision/ocr returns a friendly error
- photo-capture failures surface gracefully
- empty results don't crash the loop
"""

from __future__ import annotations

from typing import Any

import pytest
from gilbert_plugin_mentra.camera_tool import (
    FOCUS_GENERAL,
    TOOL_NAME,
    camera_tool_definition,
    execute_camera_tool,
)

# ── Stubs ───────────────────────────────────────────────────────────


class _StubCamera:
    """Stand-in for the live ``CameraManager`` — captures whether
    ``take_photo`` was called and returns whatever was pre-set as
    the photo URL."""

    def __init__(self, photo_url: str = "https://photos.mentra/abc.jpg") -> None:
        self.calls: list[dict[str, Any]] = []
        self._photo_url = photo_url
        self.raise_on_take: Exception | None = None

    async def take_photo(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_on_take is not None:
            raise self.raise_on_take

        # Minimal PhotoData-shape mock — ``url`` is the only field
        # the tool reads.
        class _Photo:
            url = self._photo_url

        return _Photo()


class _StubCapabilities:
    def __init__(self, has_camera: bool = True) -> None:
        self.has_camera = has_camera


class _StubSession:
    def __init__(
        self,
        *,
        has_camera: bool = True,
        photo_url: str = "https://photos.mentra/abc.jpg",
    ) -> None:
        self.session_id = "sess_001"
        self.capabilities = _StubCapabilities(has_camera=has_camera)
        self.camera = _StubCamera(photo_url=photo_url)


class _StubVision:
    def __init__(self, response: str = "A red exit sign.") -> None:
        self.calls: list[tuple[bytes, str]] = []
        self.response = response
        self.raise_on_call: Exception | None = None

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        self.calls.append((image_bytes, media_type))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


class _StubOCR:
    def __init__(self, response: str = "EXIT") -> None:
        self.calls: list[bytes] = []
        self.response = response
        self.raise_on_call: Exception | None = None

    async def extract_text(self, image_bytes: bytes) -> str:
        self.calls.append(image_bytes)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


class _StubResponse:
    def __init__(
        self,
        *,
        content: bytes = b"FAKEJPEGBYTES",
        content_type: str = "image/jpeg",
    ) -> None:
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


class _StubHttpClient:
    def __init__(
        self,
        *,
        content: bytes = b"FAKEJPEGBYTES",
        content_type: str = "image/jpeg",
    ) -> None:
        self._content = content
        self._content_type = content_type
        self.gets: list[str] = []

    async def __aenter__(self) -> _StubHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str) -> _StubResponse:
        self.gets.append(url)
        return _StubResponse(
            content=self._content, content_type=self._content_type
        )


def _http_factory(
    *,
    content: bytes = b"FAKEJPEGBYTES",
    content_type: str = "image/jpeg",
) -> Any:
    """Helper to build the no-arg callable ``execute_camera_tool``
    expects when an http_client_factory is injected."""
    def _make() -> _StubHttpClient:
        return _StubHttpClient(content=content, content_type=content_type)

    return _make


# ── Tests ───────────────────────────────────────────────────────────


def test_camera_tool_definition_shape() -> None:
    """The tool definition must carry name + description + the focus
    param. The LLM's tool-list rendering depends on these being
    present + the right type."""
    td = camera_tool_definition()
    assert td.name == TOOL_NAME
    assert "smart-glasses" in td.description.lower() or "glasses" in td.description.lower()
    assert len(td.parameters) == 1
    assert td.parameters[0].name == "focus"
    # Slash autocomplete metadata so the operator can also trigger
    # from chat for debugging.
    assert td.slash_command == "see"
    assert td.slash_help


@pytest.mark.asyncio
async def test_focus_text_routes_to_ocr() -> None:
    """focus='text' must:
    - call camera.take_photo
    - download the bytes
    - call OCR (not vision)
    - return the OCR text verbatim
    """
    session = _StubSession()
    vision = _StubVision()
    ocr = _StubOCR(response="EXIT — PUSH")

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert result == "EXIT — PUSH"
    assert len(session.camera.calls) == 1
    assert len(ocr.calls) == 1
    assert ocr.calls[0] == b"FAKEJPEGBYTES"
    # Vision must NOT have been called.
    assert vision.calls == []


@pytest.mark.asyncio
async def test_focus_general_routes_to_vision() -> None:
    """focus='general' is the default scene-understanding path."""
    session = _StubSession()
    vision = _StubVision(response="A cozy living room with a cat.")
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": FOCUS_GENERAL},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert result == "A cozy living room with a cat."
    assert len(vision.calls) == 1
    assert vision.calls[0][0] == b"FAKEJPEGBYTES"
    assert vision.calls[0][1] == "image/jpeg"
    assert ocr.calls == []


@pytest.mark.asyncio
async def test_focus_default_is_general() -> None:
    """No focus arg → general path. Important: the LLM's tool call
    may omit optional args entirely."""
    session = _StubSession()
    vision = _StubVision(response="A street with cars.")
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert result == "A street with cars."
    assert len(vision.calls) == 1


@pytest.mark.asyncio
async def test_unknown_focus_falls_back_to_general() -> None:
    """LLMs sometimes invent param values. Unknown focus shouldn't
    fail — degrade to general so the user gets SOMETHING useful."""
    session = _StubSession()
    vision = _StubVision()
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "blueberry"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    # Vision was called, OCR wasn't.
    assert len(vision.calls) == 1
    assert ocr.calls == []
    assert result  # non-empty


@pytest.mark.asyncio
async def test_no_camera_capability_returns_friendly_error() -> None:
    """Display-only glasses (Even Realities G1 etc.) have no camera.
    Tool must refuse with a user-facing explanation rather than
    raising — the LLM needs to apologize naturally."""
    session = _StubSession(has_camera=False)
    vision = _StubVision()
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "camera" in result.lower()
    # Never called the camera in the first place.
    assert session.camera.calls == []


@pytest.mark.asyncio
async def test_missing_ocr_returns_friendly_error_on_text_focus() -> None:
    """focus='text' without an OCR backend can't fall back to
    vision (vision is for descriptions, not extraction). Tell the
    LLM what's missing."""
    session = _StubSession()
    vision = _StubVision()
    ocr = None

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "ocr" in result.lower()
    # Don't bother snapping the photo if we can't process it.
    assert session.camera.calls == []


@pytest.mark.asyncio
async def test_missing_vision_returns_friendly_error_on_general_focus() -> None:
    session = _StubSession()
    vision = None
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "general"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "vision" in result.lower()
    assert session.camera.calls == []


@pytest.mark.asyncio
async def test_photo_capture_timeout_returns_friendly_error() -> None:
    session = _StubSession()
    session.camera.raise_on_take = TimeoutError()
    vision = _StubVision()
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "in time" in result.lower() or "timed" in result.lower() or "didn't respond" in result.lower()
    assert ocr.calls == []  # never reached download


@pytest.mark.asyncio
async def test_ocr_empty_result_returns_friendly_hint() -> None:
    """OCR sometimes returns "" for blurry / no-text photos. The
    bare empty string would be a useless tool result — convert to
    a guidance string the LLM can speak as advice ('aim closer')."""
    session = _StubSession()
    vision = _StubVision()
    ocr = _StubOCR(response="")

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "couldn't read" in result.lower() or "no text" in result.lower()


@pytest.mark.asyncio
async def test_vision_empty_result_returns_friendly_hint() -> None:
    session = _StubSession()
    vision = _StubVision(response="")
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "couldn't" in result.lower() or "again" in result.lower()


@pytest.mark.asyncio
async def test_media_type_passed_through_to_vision() -> None:
    """If the cloud's response includes a content-type other than
    JPEG, that should propagate to the vision backend so it can
    decode correctly. Anthropic + OpenAI vision both require an
    accurate media_type."""
    session = _StubSession()
    vision = _StubVision(response="A grayscale PNG.")
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(content_type="image/png"),
    )

    assert result == "A grayscale PNG."
    assert vision.calls[0][1] == "image/png"


@pytest.mark.asyncio
async def test_missing_photo_url_returns_friendly_error() -> None:
    """Cloud occasionally accepts the photo_request but responds
    without a photoUrl (transient backend issue). Surface that as
    actionable user feedback rather than a stack trace."""
    session = _StubSession(photo_url="")
    vision = _StubVision()
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert "url" in result.lower() or "try again" in result.lower()
    assert ocr.calls == []
