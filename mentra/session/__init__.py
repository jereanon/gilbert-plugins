"""Session layer — Transport ABC + MentraSession + per-feature managers.

A ``MentraSession`` is the Python analog of the upstream
``MentraSession`` TS class: owns one WebSocket connection back to
Mentra Cloud, runs the handshake, routes inbound frames to
registered handlers, and exposes manager objects for outbound
commands. One session per glasses-app pairing.
"""

from .session import MentraSession, MentraSessionConfig
from .transport import Transport, TransportState, WebSocketTransport

__all__ = [
    "MentraSession",
    "MentraSessionConfig",
    "Transport",
    "TransportState",
    "WebSocketTransport",
]
