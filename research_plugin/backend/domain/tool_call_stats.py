"""Pure rollups for filtered tool-call rows."""

from __future__ import annotations

from statistics import quantiles
from typing import Any


def tool_call_totals(calls: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "calls": len(calls),
        "sent_chars": sum(_int(row.get("sent_chars")) for row in calls),
        "received_chars": sum(_int(row.get("received_chars")) for row in calls),
        "error_calls": sum(1 for row in calls if row.get("status") == "error"),
    }


def by_tool(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for call in calls:
        tool = str(call.get("tool") or "")
        bucket = buckets.setdefault(
            tool,
            {
                "tool": tool,
                "calls": 0,
                "error_calls": 0,
                "sent_chars": 0,
                "received_chars": 0,
                "max_received_chars": 0,
                "max_sent_chars": 0,
                "_duration_sum": 0,
                "max_duration_ms": 0,
                "_received_samples": [],
                "last_ts": None,
            },
        )
        sent = _int(call.get("sent_chars"))
        received = _int(call.get("received_chars"))
        duration = _int(call.get("duration_ms"))
        bucket["calls"] += 1
        bucket["error_calls"] += 1 if call.get("status") == "error" else 0
        bucket["sent_chars"] += sent
        bucket["received_chars"] += received
        bucket["max_received_chars"] = max(bucket["max_received_chars"], received)
        bucket["max_sent_chars"] = max(bucket["max_sent_chars"], sent)
        bucket["_duration_sum"] += duration
        bucket["max_duration_ms"] = max(bucket["max_duration_ms"], duration)
        bucket["_received_samples"].append(received)
        ts = call.get("ts")
        if bucket["last_ts"] is None or (ts is not None and ts > bucket["last_ts"]):
            bucket["last_ts"] = ts
    return sorted(
        (_finalize_bucket(bucket) for bucket in buckets.values()),
        key=lambda row: row["received_chars"],
        reverse=True,
    )


def percentile(values: list[int], q: int) -> int:
    samples = sorted(_int(value) for value in values)
    if not samples:
        return 0
    if len(samples) == 1:
        return samples[0]
    if q <= 0:
        return samples[0]
    if q >= 100:
        return samples[-1]
    cuts = quantiles(samples, n=100, method="inclusive")
    return int(round(cuts[q - 1]))


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    count = _int(bucket["calls"]) or 1
    samples = sorted(bucket.pop("_received_samples"))
    duration_sum = bucket.pop("_duration_sum")
    bucket["avg_received_chars"] = round(bucket["received_chars"] / count)
    bucket["avg_sent_chars"] = round(bucket["sent_chars"] / count)
    bucket["avg_duration_ms"] = round(duration_sum / count)
    bucket["p50_received_chars"] = percentile(samples, 50)
    bucket["p95_received_chars"] = percentile(samples, 95)
    return bucket


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
