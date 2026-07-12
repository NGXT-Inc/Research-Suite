"""The review-pinning snapshot id: a byte-stable equality key.

Compared for equality in services/reviews.py and parsed back by
snapshot_from_id, so this format is load-bearing and must not drift.
"""
from __future__ import annotations
from typing import Any

def review_snapshot_id(*, target_type: str, target: dict[str, Any]) -> str:
    """`type|id|status|attempt|sorted-comma-joined-resource-tokens`.

    `target` is a get_state() dict with id/status/attempt_index and
    current_attempt_resources. Field order and token format are an
    equality key — keep byte-identical."""
    resource_tokens = [
        f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role', '')}:{res.get('association_attempt_index', 0)}"
        for res in target.get("current_attempt_resources", [])
    ]
    return "|".join(
        [
            target_type,
            target["id"],
            target["status"],
            str(target["attempt_index"]),
            ",".join(sorted(resource_tokens)),
        ]
    )


def snapshot_from_id(*, snapshot_id: str) -> dict[str, Any]:
    if "|" not in snapshot_id:
        target_type, _, target_id = snapshot_id.partition(":")
        return {"target_type": target_type, "target_id": target_id, "resources": []}
    parts = snapshot_id.split("|", 4)
    resources = []
    for token in (parts[4].split(",") if len(parts) > 4 and parts[4] else []):
        try:
            resource_and_version, role, attempt_index = token.rsplit(":", 2)
            resource_id, version_ref = resource_and_version.split(":", 1)
        except ValueError:
            resources.append({"raw": token})
            continue
        item: dict[str, Any] = {
            "resource_id": resource_id,
            "role": role,
            "attempt_index": _int_or_zero(value=attempt_index),
        }
        if version_ref.startswith("rver_"):
            item["version_id"] = version_ref
        else:
            item["version_token"] = version_ref
        resources.append(item)
    return {
        "target_type": parts[0] if len(parts) > 0 else "",
        "target_id": parts[1] if len(parts) > 1 else "",
        "status": parts[2] if len(parts) > 2 else "",
        "attempt_index": _int_or_zero(value=parts[3]) if len(parts) > 3 else 0,
        "resources": resources,
    }


def _int_or_zero(*, value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
