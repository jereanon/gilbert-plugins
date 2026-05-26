"""Wire-format layer for the Mentra cloud protocol.

JSON-over-WebSocket (text frames) for control + data, raw PCM 16
kHz mono 16-bit in binary frames for audio. Mirrors the upstream
TypeScript SDK's ``types/`` directory — field names match exactly
so cross-referencing the canonical client stays trivial.
"""
