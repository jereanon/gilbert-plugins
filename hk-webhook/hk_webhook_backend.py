"""Generic health-data webhook backend.

Accepts JSON payloads in the standard shape used by ``apple-health``
(see std-plugins/apple-health/) without the HealthKit-identifier
mapping step — callers are expected to send ``MetricType`` enum
values directly. Unknown metric types are dropped with an INFO log
line; the backend never returns an error for an unknown type, since
that would let one misbehaving metric break the whole batch.

Per the spec §4.5 contract: ``hk-webhook`` declares NO ``extra``
keys. The back-channel for caller metadata is ``source_event_id``,
NOT arbitrary string blobs. Any ``extra`` key in the payload is
silently dropped before the metric reaches storage.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gilbert.interfaces.health import (
    HealthBackend,
    HealthMetric,
    MetricPayloadError,
    MetricType,
    parse_metric_payload,
)

logger = logging.getLogger(__name__)


class HKWebhookBackend(HealthBackend):
    backend_name = "hk-webhook"

    def __init__(self) -> None:
        self._initialized = False

    @property
    def supports_push(self) -> bool:
        return True

    async def initialize(self, config: dict[str, Any]) -> None:
        self._initialized = True

    async def close(self) -> None:
        self._initialized = False

    async def parse_webhook(
        self,
        user_id: str,
        body: bytes,
        headers: dict[str, str],
    ) -> list[HealthMetric]:
        """Parse a JSON delivery into ``HealthMetric`` rows.

        Accepted shapes:
        - ``{"metrics": [{...}, ...]}`` (preferred)
        - ``[{...}, ...]`` (top-level array — same per-item shape)
        - ``{...}`` (a single metric object)

        Each item must include ``type``, ``value``, ``unit``, and
        ``recorded_at`` (per ``parse_metric_payload``). Items with
        unknown ``type`` values are dropped with an INFO log;
        items that fail other validation drop with a DEBUG log.
        """
        if not body:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"hk-webhook: malformed JSON: {exc}") from exc

        if isinstance(data, dict) and "metrics" in data:
            items = data.get("metrics") or []
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            raise ValueError("hk-webhook: payload must be an array or object")

        if not isinstance(items, list):
            raise ValueError("hk-webhook: 'metrics' must be a list")

        out: list[HealthMetric] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                logger.debug("hk-webhook: skipping non-object item")
                continue
            # Per spec §4.5: hk-webhook strips all ``extra`` keys —
            # the back-channel for caller metadata is source_event_id.
            sanitized = dict(raw_item)
            sanitized.pop("extra", None)
            try:
                metric = parse_metric_payload(
                    sanitized,
                    user_id=user_id,
                    backend=self.backend_name,
                )
                out.append(metric)
            except MetricPayloadError as exc:
                msg = str(exc).lower()
                if "unknown metric type" in msg:
                    logger.info(
                        "hk-webhook: dropping unknown metric type for "
                        "user %s: %s",
                        user_id,
                        exc,
                    )
                else:
                    logger.debug(
                        "hk-webhook: dropping malformed metric for user %s: %s",
                        user_id,
                        exc,
                    )
                continue
        return out

    def supported_metrics(self) -> set[MetricType]:
        # Generic backend — claims every known type so the SPA shows
        # all of them as "potentially available."
        return set(MetricType)

