"""Shared Anthropic API key registry — module-local.

The Anthropic plugin ships three backends today (AI / Vision / OCR)
that all hit the same ``api.anthropic.com`` with the same kind of
API key. Forcing the user to paste the key three times across
Settings → AI, Settings → Vision, and Settings → OCR is busywork
— if any one of them has a key, the others should reuse it.

This module holds the most-recently-seen non-empty key in a
module-level variable. Each backend's ``initialize()`` calls
``register_anthropic_api_key`` when it sees a non-empty config
value; ``get_shared_anthropic_api_key`` returns the latest one any
sibling backend recorded.

Resolution order at call-time (per backend):
  1. Backend's own ``api_key`` config value (operator explicitly set
     it for this backend — wins).
  2. Shared key from any sibling backend that initialized with one.
  3. Empty → backend reports ``available=False`` and consumers get
     the actionable "API key not configured" guidance.

Lifetime: process-local. Restarts clear the registry; both backends
re-populate it from their config on startup. Multi-tenant deploys
where different users want different keys per backend MUST set the
per-backend config explicitly — the shared registry only kicks in
when the per-backend config is empty.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "get_shared_anthropic_api_key",
    "register_anthropic_api_key",
]


_shared_api_key: str = ""


def register_anthropic_api_key(api_key: str, *, source: str) -> None:
    """Record an Anthropic API key seen by one of the backends.

    No-op when ``api_key`` is empty. ``source`` is a short label
    used in the diagnostic log line (e.g. ``"ai"``, ``"vision"``,
    ``"ocr"``) so production journals make it clear which backend
    seeded the shared key.
    """
    global _shared_api_key
    if not api_key:
        return
    if api_key == _shared_api_key:
        return
    had_previous = bool(_shared_api_key)
    _shared_api_key = api_key
    logger.info(
        "Anthropic shared key %s by %s backend",
        "updated" if had_previous else "seeded",
        source,
    )


def get_shared_anthropic_api_key() -> str:
    """Return the most-recently-seen Anthropic API key from any
    sibling backend, or ``""`` if none has been registered yet.
    """
    return _shared_api_key


def _reset_for_testing() -> None:
    """Test-only: clear the registry so per-test isolation holds."""
    global _shared_api_key
    _shared_api_key = ""
