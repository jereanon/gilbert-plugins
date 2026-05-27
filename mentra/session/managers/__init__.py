"""Per-feature managers — the developer-facing API surface a session
exposes (``session.display.show_text_wall(...)``, etc.). Each
manager wraps one cloud subsystem; the underlying wire format and
dispatch live in ``session/session.py``.

v1: transcription, button presses, display layouts, dashboard
cards, TTS.

v1.1 (this commit): LED, location, mic (raw PCM + VAD), camera
(photo capture + managed livestream). Reconnect / park handling
added on the session side.
"""

from .button import ButtonManager
from .camera import CameraManager
from .dashboard import DashboardManager
from .display import DisplayManager
from .led import LedManager
from .location import LocationManager
from .mic import MicManager
from .speaker import SpeakerManager
from .transcription import TranscriptionManager

__all__ = [
    "ButtonManager",
    "CameraManager",
    "DashboardManager",
    "DisplayManager",
    "LedManager",
    "LocationManager",
    "MicManager",
    "SpeakerManager",
    "TranscriptionManager",
]
