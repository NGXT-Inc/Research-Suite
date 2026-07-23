"""The review-pinning snapshot id: a byte-stable equality key.

Compared for equality in research_core/reviews.py and parsed back by
snapshot_from_id, so this format is load-bearing and must not drift.
"""
from __future__ import annotations
from typing import Any

def review_snapshot_id(*, target_type: str, target: dict[str, Any]) -> str:
    """`type|id|status|attempt|sorted-comma-joined-artifact-tokens`.

    `target` is a get_state() dict with id/status/attempt_index and
    current_attempt_artifacts (artifact-backed records). Each token is
    `artifact_id:role:attempt` — resubmitting mints a new artifact id, so the
    snapshot invalidates without any content fingerprint. Field order and
    token format are an equality key — keep byte-identical."""
    artifact_tokens = [
        f"{res['id']}:{res.get('role', '')}:{res.get('attempt_index', 0)}"
        for res in target.get("current_attempt_artifacts", [])
    ]
    return "|".join(
        [
            target_type,
            target["id"],
            target["status"],
            str(target["attempt_index"]),
            ",".join(sorted(artifact_tokens)),
        ]
    )


def snapshot_from_id(*, snapshot_id: str) -> dict[str, Any]:
    if "|" not in snapshot_id:
        target_type, _, target_id = snapshot_id.partition(":")
        return {"target_type": target_type, "target_id": target_id, "artifacts": []}
    parts = snapshot_id.split("|", 4)
    artifacts = []
    for token in (parts[4].split(",") if len(parts) > 4 and parts[4] else []):
        try:
            artifact_id, role, attempt_index = token.rsplit(":", 2)
        except ValueError:
            artifacts.append({"raw": token})
            continue
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "role": role,
                "attempt_index": _int_or_zero(value=attempt_index),
            }
        )
    return {
        "target_type": parts[0] if len(parts) > 0 else "",
        "target_id": parts[1] if len(parts) > 1 else "",
        "status": parts[2] if len(parts) > 2 else "",
        "attempt_index": _int_or_zero(value=parts[3]) if len(parts) > 3 else 0,
        "artifacts": artifacts,
    }


def _int_or_zero(*, value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
