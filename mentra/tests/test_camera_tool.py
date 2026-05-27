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
    def __init__(
        self, response: str = "A red exit sign.", available: bool = True
    ) -> None:
        # (bytes, media_type, prompt) — exposing prompt lets tests
        # assert the camera_tool passed a focus-aware instruction
        # rather than letting the backend's operator-tuned default
        # leak in (which is what broke the live system: the default
        # said "return empty if no technical content").
        self.calls: list[tuple[bytes, str, str]] = []
        self.response = response
        self.raise_on_call: Exception | None = None
        self.available = available

    async def describe_image(
        self,
        image_bytes: bytes,
        media_type: str,
        *,
        prompt: str = "",
    ) -> str:
        self.calls.append((image_bytes, media_type, prompt))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


class _StubOCR:
    def __init__(
        self, response: str = "EXIT", available: bool = True
    ) -> None:
        self.calls: list[bytes] = []
        self.response = response
        self.raise_on_call: Exception | None = None
        self.available = available

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
    # Must pass a focus-aware prompt — never letting the backend's
    # operator-tuned default leak in. The general-scene prompt
    # specifically tells the model NOT to return empty (the prior
    # PDF-extraction default did, which broke this entire flow).
    general_prompt = vision.calls[0][2]
    assert general_prompt
    assert "never return an empty response" in general_prompt.lower()
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
async def test_face_focus_uses_face_prompt() -> None:
    """``focus='face'`` should pass the face-focused prompt (people
    descriptions + identity caveats) to vision, not the general
    scene-description prompt. This is a regression guard against the
    earlier bug where the backend's hardcoded technical-extraction
    prompt leaked into every call."""
    session = _StubSession()
    vision = _StubVision(response="A man in a blue shirt smiling.")
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "face"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    assert result == "A man in a blue shirt smiling."
    assert len(vision.calls) == 1
    face_prompt = vision.calls[0][2]
    # Face prompt mentions people / identity-handling explicitly.
    assert any(
        kw in face_prompt.lower()
        for kw in ("people", "person", "identity", "wearing")
    )
    # Same non-empty-response guarantee as the general path.
    assert "never return an empty response" in face_prompt.lower()


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


@pytest.mark.asyncio
async def test_vision_not_available_returns_admin_guidance() -> None:
    """Vision service registered but ``available=False`` (most common
    cause: Anthropic API key missing). Return concrete guidance
    pointing the admin at Settings → Vision rather than the
    ambiguous 'try again with a clearer view' message we used to
    return when describe_image returned empty bytes."""
    session = _StubSession()
    vision = _StubVision(available=False)
    ocr = _StubOCR()

    result = await execute_camera_tool(
        session=session,
        arguments={},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    # Concrete admin-action guidance + a useful fallback hint for
    # the user (OCR works for text-on-things questions).
    msg = result.lower()
    assert "vision" in msg
    assert "api key" in msg or "settings" in msg
    # OCR routing hint so the user has a way to get value out of
    # their session even while vision is broken.
    assert "ocr" in msg or "this say" in msg
    # describe_image MUST NOT have been called — we short-circuited
    # on the availability check.
    assert vision.calls == []
    # Photo MUST NOT have been taken either — no point capturing
    # bytes we know we can't process.
    assert session.camera.calls == []


@pytest.mark.asyncio
async def test_ocr_not_available_returns_admin_guidance() -> None:
    """OCR registered but backend not ready (e.g. missing language
    pack). Surface as admin-actionable error, not 'try again with
    better light'."""
    session = _StubSession()
    vision = _StubVision()
    ocr = _StubOCR(available=False)

    result = await execute_camera_tool(
        session=session,
        arguments={"focus": "text"},
        vision=vision,
        ocr=ocr,
        http_client_factory=_http_factory(),
    )

    msg = result.lower()
    assert "ocr" in msg
    assert "configured" in msg or "settings" in msg or "api key" in msg
    assert ocr.calls == []
    assert session.camera.calls == []


@pytest.mark.asyncio
async def test_backend_without_available_attr_assumed_ready() -> None:
    """Plugins / older backends might not expose an ``available``
    property. The tool defaults to True via getattr so it doesn't
    accidentally regress consumers that didn't opt into the
    availability check."""

    class _LegacyVision:
        # No ``available`` attribute on purpose. ``prompt`` is the
        # new kwarg — even legacy fakes must accept it now since the
        # camera tool always passes one.
        async def describe_image(
            self,
            image_bytes: bytes,
            media_type: str,
            *,
            prompt: str = "",
        ) -> str:
            return "A clear scene."

    session = _StubSession()
    result = await execute_camera_tool(
        session=session,
        arguments={},
        vision=_LegacyVision(),
        ocr=None,
        http_client_factory=_http_factory(),
    )

    # Legacy backend was called; tool didn't bail out on the missing
    # availability check.
    assert result == "A clear scene."
