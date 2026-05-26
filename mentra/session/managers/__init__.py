"""Per-feature managers — the developer-facing API surface a session
exposes (``session.display.show_text_wall(...)``, etc.). Each
manager wraps one cloud subsystem; the underlying wire format and
dispatch live in ``session/session.py``.

For v1 we ship the subset Gilbert's Tier 1 use cases need:
transcription, button presses, display layouts, dashboard cards,
and TTS. Camera / LED / livestream / location land in follow-up
work.
"""

from .button import ButtonManager
from .dashboard import DashboardManager
from .display import DisplayManager
from .speaker import SpeakerManager
from .transcription import TranscriptionManager

__all__ = [
    "ButtonManager",
    "DashboardManager",
    "DisplayManager",
    "SpeakerManager",
    "TranscriptionManager",
]
