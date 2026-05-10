"""Apple Health (HealthKit) webhook backend.

The iOS Shortcut posts JSON in the standard shape to
``/webhook/health/<token>``. Some payloads use HealthKit identifier
names (``HKQuantityTypeIdentifierStepCount``) — this backend
translates them to ``MetricType`` values before handing off to
``parse_metric_payload``.

Per spec §4.5 the apple-health ``extra`` whitelist contains exactly
two keys: ``device`` (HKDevice.name) and ``source_app`` (HKSource.name).
Every other key in the payload's ``extra`` dict is dropped before
the metric reaches storage.
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


# HealthKit identifier → MetricType mapping. Add a new row when a
# new MetricType is introduced AND HealthKit has a corresponding
# identifier; unknown identifiers drop with an INFO log line.
_HK_IDENTIFIER_MAP: dict[str, MetricType] = {
    # Sleep
    "HKCategoryTypeIdentifierSleepAnalysis": MetricType.SLEEP_DURATION,
    "HKCategoryValueSleepAnalysisAsleepDeep": MetricType.SLEEP_DEEP,
    "HKCategoryValueSleepAnalysisAsleepREM": MetricType.SLEEP_REM,
    "HKCategoryValueSleepAnalysisAwake": MetricType.SLEEP_AWAKE,
    # Activity
    "HKQuantityTypeIdentifierStepCount": MetricType.STEPS,
    "HKQuantityTypeIdentifierDistanceWalkingRunning": MetricType.DISTANCE,
    "HKQuantityTypeIdentifierAppleExerciseTime": MetricType.ACTIVE_MINUTES,
    "HKQuantityTypeIdentifierActiveEnergyBurned": MetricType.CALORIES_BURNED,
    # Heart
    "HKQuantityTypeIdentifierRestingHeartRate": MetricType.HEART_RATE_RESTING,
    "HKQuantityTypeIdentifierHeartRate": MetricType.HEART_RATE_AVG,
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": MetricType.HRV,
    "HKQuantityTypeIdentifierOxygenSaturation": MetricType.SPO2,
    # Body
    "HKQuantityTypeIdentifierBodyMass": MetricType.WEIGHT,
    "HKQuantityTypeIdentifierBodyFatPercentage": MetricType.BODY_FAT,
    "HKQuantityTypeIdentifierLeanBodyMass": MetricType.LEAN_MASS,
    "HKQuantityTypeIdentifierBodyMassIndex": MetricType.BMI,
    # Vitals
    "HKQuantityTypeIdentifierBloodPressureSystolic": MetricType.BLOOD_PRESSURE_SYS,
    "HKQuantityTypeIdentifierBloodPressureDiastolic": MetricType.BLOOD_PRESSURE_DIA,
    "HKQuantityTypeIdentifierBodyTemperature": MetricType.BODY_TEMPERATURE,
    "HKQuantityTypeIdentifierRespiratoryRate": MetricType.RESPIRATORY_RATE,
    "HKQuantityTypeIdentifierVO2Max": MetricType.VO2_MAX,
}


_ALLOWED_EXTRA_KEYS = frozenset({"device", "source_app"})


class AppleHealthBackend(HealthBackend):
    backend_name = "apple-health"

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
        """Parse an iOS Shortcut delivery into ``HealthMetric`` rows.

        Each item may use either:
        - ``"type": "steps"`` (a ``MetricType`` enum value), OR
        - ``"type": "HKQuantityTypeIdentifierStepCount"`` (a HealthKit
          identifier — translated by ``_HK_IDENTIFIER_MAP``)

        Items with unknown HealthKit identifiers are dropped with an
        INFO log; items with malformed values drop with DEBUG. Per
        spec §4.5 the ``extra`` whitelist allows ``device`` and
        ``source_app`` only — everything else gets dropped.
        """
        if not body:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"apple-health: malformed JSON: {exc}") from exc

        if isinstance(data, dict) and "metrics" in data:
            items = data.get("metrics") or []
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            raise ValueError("apple-health: payload must be an array or object")

        if not isinstance(items, list):
            raise ValueError("apple-health: 'metrics' must be a list")

        out: list[HealthMetric] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                logger.debug("apple-health: skipping non-object item")
                continue
            sanitized = dict(raw_item)
            # Translate HealthKit identifiers.
            type_raw = str(sanitized.get("type") or "")
            if type_raw.startswith("HK"):
                mapped = _HK_IDENTIFIER_MAP.get(type_raw)
                if mapped is None:
                    logger.info(
                        "apple-health: dropping unknown HealthKit identifier "
                        "for user %s: %s",
                        user_id,
                        type_raw,
                    )
                    continue
                sanitized["type"] = mapped.value

            # Apply ``extra`` whitelist BEFORE the parser so the
            # parser's per-key length caps don't accidentally let a
            # disallowed key through (e.g. one trimmed below 64 chars).
            extra_raw = sanitized.get("extra") or {}
            if isinstance(extra_raw, dict):
                sanitized["extra"] = {
                    str(k): str(v)
                    for k, v in extra_raw.items()
                    if str(k) in _ALLOWED_EXTRA_KEYS
                }
            else:
                sanitized["extra"] = {}

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
                        "apple-health: dropping unknown metric type for "
                        "user %s: %s",
                        user_id,
                        exc,
                    )
                else:
                    logger.debug(
                        "apple-health: dropping malformed metric for user "
                        "%s: %s",
                        user_id,
                        exc,
                    )
                continue
        return out

    def supported_metrics(self) -> set[MetricType]:
        # Every MetricType present in the HealthKit map.
        return set(_HK_IDENTIFIER_MAP.values()) | {
            MetricType.STEPS,
            MetricType.WEIGHT,
            MetricType.SLEEP_DURATION,
        }

