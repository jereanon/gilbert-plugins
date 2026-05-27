"""Camera-driven AI tool for the Mentra plugin.

Exposes ``look_at_what_im_seeing`` — an LLM tool callable mid-voice-
session that snaps a photo through the glasses' camera and runs the
result through Gilbert's existing vision / OCR pipeline. The tool
returns the description / extracted text as a string; the LLM
weaves that into its next reply, which voice_brain then synthesizes
and plays through the glasses speaker.

Conceptually:

    user (via mic):    "Hey Gilbert, what does that sign say?"
    LLM (chooses tool):  look_at_what_im_seeing(focus="text")
    this module:       session.camera.take_photo() → photo_url
                       httpx.get(photo_url) → JPEG bytes
                       ocr.extract_text(bytes) → "EXIT — PUSH"
    LLM (next turn):   "It says 'EXIT — PUSH'."
    voice_brain TTS → glasses speaker

This module owns the photo-fetch + capability dispatch logic. The
tool's lifecycle integration (registration, ContextVar gating,
``ai_tools`` capability advertisement) lives in
``mentra_service.MentraService`` so the service stays the single
``ToolProvider`` Gilbert's AI service discovers — same shape
voice-agent uses for its ``end_conversation`` tool.

Video (managed-stream) capture is deliberately not exposed yet —
the ``CameraManager.start_managed_stream`` API exists but routing
a live video feed to a vision model is a streaming-and-cost problem
we don't have a use case shaped well enough for. The extension
point is here when we do.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.ocr import OCRProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.vision import VisionProvider

logger = logging.getLogger(__name__)


__all__ = [
    "FOCUS_GENERAL",
    "FOCUS_TEXT",
    "FOCUS_FACE",
    "TOOL_NAME",
    "camera_tool_definition",
    "execute_camera_tool",
]


# Tool name + focus modes. Keep these stable — they're part of the
# AI's tool surface and changing them retroactively breaks any
# saved conversation history that referenced them.
TOOL_NAME = "look_at_what_im_seeing"
FOCUS_GENERAL = "general"
FOCUS_TEXT = "text"
FOCUS_FACE = "face"

# Cap how long we'll wait for the cloud's photo response. The
# upstream SDK's default is 30s; we leave that in place at the
# manager layer and only override here when latency matters more
# than reliability (which, for a voice-loop tool, it does — the
# user is mid-conversation and 30s of silence is unacceptable).
_PHOTO_TIMEOUT_S = 15.0

# Cap how long we'll spend downloading the photo bytes from the
# cloud's hosted URL. Real-world median is ~500ms but a flaky
# carrier link can spike; 10s gives ample headroom without holding
# the user forever.
_PHOTO_DOWNLOAD_TIMEOUT_S = 10.0


def camera_tool_definition() -> ToolDefinition:
    """The ``ToolDefinition`` MentraService.get_tools() returns when
    the LLM is mid-glasses-session and the active session has a
    camera-capable device."""
    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Capture a photo through the user's smart-glasses camera "
            "and have an AI model describe (or read text from) what "
            "the user is currently looking at. Use this whenever the "
            "user asks about their surroundings, what's in front of "
            "them, what a sign or document says, who they're with, "
            "or wants help identifying something they can physically "
            "see. ONLY usable during an active glasses voice session — "
            "won't work when the user is in regular chat. "
            "\n\n"
            "The ``focus`` parameter routes the captured photo to the "
            "right backend: "
            "\n"
            "  - ``general`` (default): describe the scene with a "
            "vision model. Best for 'what am I looking at?', 'what's "
            "in front of me?', 'is this safe to eat?', open-ended "
            "scene-understanding questions. "
            "\n"
            "  - ``text``: extract text via OCR. Best for signs, "
            "menus, receipts, packaging labels, document pages, error "
            "messages on screens — anything printed where the user "
            "wants to know what it SAYS. "
            "\n"
            "  - ``face``: identify familiar people. Falls back to a "
            "general scene description when no face-recognition "
            "backend is configured."
            "\n\n"
            "Returns a string describing what was seen (or the "
            "extracted text). The user does NOT hear the raw tool "
            "output — incorporate it into your spoken reply naturally. "
            "E.g. if OCR returns 'EXIT', SAY 'That sign says EXIT' "
            "rather than just 'EXIT'."
        ),
        parameters=[
            ToolParameter(
                name="focus",
                type=ToolParameterType.STRING,
                description=(
                    "How to interpret the photo. One of: "
                    "'general' (default — describe the scene), "
                    "'text' (OCR for signs / menus / documents), "
                    "'face' (recognize familiar people; degrades to "
                    "general if no face-recognition backend)."
                ),
                required=False,
            ),
        ],
        slash_command="see",
        slash_help=(
            "Ask Gilbert what your glasses are looking at "
            "(needs an active Mentra session)."
        ),
    )


async def execute_camera_tool(
    *,
    session: Any,                # MentraSession (avoid circular import)
    arguments: dict[str, Any],
    vision: VisionProvider | None,
    ocr: OCRProvider | None,
    http_client_factory: Any | None = None,
) -> str:
    """Run the ``look_at_what_im_seeing`` tool against ``session``.

    ``http_client_factory`` is optional and exists for tests — when
    omitted, we lazy-import ``httpx`` and use a fresh AsyncClient per
    call. Tests inject a stub that returns canned bytes without a
    network round-trip.

    Returns a user-friendly string that becomes part of the LLM's
    tool-result context. Errors are returned as user-friendly strings
    too (not raised) — the LLM should be able to apologize naturally
    rather than getting a traceback it can't reason about.
    """
    # ── Guard rails ─────────────────────────────────────────────
    caps = getattr(session, "capabilities", None)
    if caps is not None and not getattr(caps, "has_camera", False):
        return (
            "Your glasses don't have a camera, so I can't take a "
            "photo. (Mentra Live has one; pure-display models like "
            "Even Realities G1 don't.)"
        )

    camera = getattr(session, "camera", None)
    if camera is None:
        return (
            "I can't access your glasses' camera right now — the "
            "session isn't fully wired up."
        )

    focus_raw = str(arguments.get("focus") or FOCUS_GENERAL).strip().lower()
    if focus_raw not in (FOCUS_GENERAL, FOCUS_TEXT, FOCUS_FACE):
        # Unknown focus → degrade to general rather than rejecting.
        # The LLM is allowed to be creative with the param value; we
        # do the right thing instead of pedantically failing.
        logger.info(
            "look_at_what_im_seeing: unknown focus=%r — falling back to general",
            focus_raw,
        )
        focus_raw = FOCUS_GENERAL

    if focus_raw == FOCUS_TEXT and ocr is None:
        return (
            "I don't have an OCR backend configured, so I can't "
            "read text from photos. Ask the admin to enable an OCR "
            "service in Settings."
        )
    if focus_raw in (FOCUS_GENERAL, FOCUS_FACE) and vision is None:
        return (
            "I don't have a vision backend configured, so I can't "
            "describe what's in the photo. Ask the admin to enable "
            "a vision service in Settings."
        )

    # ── Capture ─────────────────────────────────────────────────
    try:
        photo = await camera.take_photo(timeout=_PHOTO_TIMEOUT_S)
    except TimeoutError:
        return (
            "The camera didn't respond in time. The user's glasses "
            "might be off-line or the cloud is slow right now."
        )
    except RuntimeError as exc:
        return f"Camera refused the shot: {exc}"
    except Exception as exc:
        logger.exception(
            "look_at_what_im_seeing: take_photo raised "
            "(session=%s)",
            getattr(session, "session_id", "?"),
        )
        return f"I couldn't take a photo: {exc}"

    photo_url = str(getattr(photo, "url", "") or "")
    if not photo_url:
        return (
            "The cloud accepted the photo request but didn't return "
            "a URL to download it from. Try again."
        )

    # ── Download bytes ──────────────────────────────────────────
    try:
        image_bytes, media_type = await _download_photo(
            photo_url, http_client_factory=http_client_factory
        )
    except Exception as exc:
        logger.exception(
            "look_at_what_im_seeing: photo download failed url=%s",
            photo_url,
        )
        return f"I couldn't download the photo from the cloud: {exc}"

    if not image_bytes:
        return "The cloud returned an empty photo. Try again."

    logger.info(
        "look_at_what_im_seeing: captured photo session=%s "
        "focus=%s bytes=%d media_type=%s",
        getattr(session, "session_id", "?"),
        focus_raw,
        len(image_bytes),
        media_type,
    )

    # ── Dispatch to vision / OCR ────────────────────────────────
    if focus_raw == FOCUS_TEXT:
        assert ocr is not None  # guarded above
        try:
            text = await ocr.extract_text(image_bytes)
        except Exception as exc:
            logger.exception("OCR extract_text raised")
            return f"OCR failed: {exc}"
        text = (text or "").strip()
        if not text:
            return (
                "I took the photo but couldn't read any text in it. "
                "Try aiming closer to the text, or in better light."
            )
        return text

    # FOCUS_GENERAL / FOCUS_FACE — both routed to vision today.
    # When a face-recognition backend gets added, branch on FOCUS_FACE
    # here before the vision fallback.
    assert vision is not None  # guarded above
    try:
        description = await vision.describe_image(image_bytes, media_type)
    except Exception as exc:
        logger.exception("Vision describe_image raised")
        return f"I couldn't analyze the photo: {exc}"
    description = (description or "").strip()
    if not description:
        return (
            "I took the photo but couldn't describe it. Try again "
            "with a clearer view."
        )
    return description


async def _download_photo(
    url: str,
    *,
    http_client_factory: Any | None = None,
) -> tuple[bytes, str]:
    """Fetch the photo bytes from a Mentra Cloud-hosted URL.

    Returns ``(bytes, media_type)``. ``media_type`` is parsed from
    the response's Content-Type header, with a sensible fallback to
    ``image/jpeg`` (the cloud's default photo format) when missing.

    ``http_client_factory`` is a no-arg callable returning an
    ``httpx.AsyncClient``-shaped object (must support ``async with``
    + ``await client.get(url, timeout=...)``). Tests inject a stub;
    production lazy-imports httpx.
    """
    if http_client_factory is None:
        import httpx

        def _default_factory() -> Any:
            return httpx.AsyncClient(timeout=_PHOTO_DOWNLOAD_TIMEOUT_S)

        http_client_factory = _default_factory

    async with http_client_factory() as client:
        resp = await client.get(url)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        # httpx exposes content as bytes; tests' stubs do the same.
        data = resp.content
        media_type = "image/jpeg"
        ct = ""
        try:
            ct = str(resp.headers.get("content-type") or "")
        except Exception:
            ct = ""
        if ct:
            # Strip any ``; charset=...`` suffix; keep just the mime.
            media_type = ct.split(";", 1)[0].strip() or media_type
        return bytes(data), media_type
