from __future__ import annotations

from datetime import datetime, timezone


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _truncate_to_millisecond(value: datetime) -> datetime:
    utc_value = _as_utc(value)
    return utc_value.replace(microsecond=(utc_value.microsecond // 1000) * 1000)


def _timestamp_adjustment_reason(
    *,
    precision_adjusted: bool,
    collision_adjustment_ms: int,
    cutoff_capped: bool = False,
) -> str | None:
    reasons: list[str] = []
    if precision_adjusted:
        reasons.append("millisecond_precision")
    if collision_adjustment_ms:
        reasons.append("timestamp_collision")
    if cutoff_capped:
        reasons.append("cutoff_capped")
    return ",".join(reasons) if reasons else None
