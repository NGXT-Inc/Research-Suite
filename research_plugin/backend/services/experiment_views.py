"""Experiment projections for agent-facing tool surfaces."""

from __future__ import annotations

from typing import Any


# Agent-facing projection of get_state. get_state is the "give me the detail"
# call, so unlike status_and_next we KEEP the substance (review findings/notes,
# intent, conclusion, the resource list). We only drop the pure waste: the
# duplicate all-attempts `resources` list (a byte-for-byte copy of
# current_attempt_resources for single-attempt experiments), per-resource
# derived/bookkeeping fields (version_token — itself path:mtime:mtime:size —,
# mtime_ns, the two usually-equal *_version_id, the three timestamps, repeated
# project_id, constant created_by/git_commit/association_attempt_index), and
# review internals (target_snapshot_id, request_id/session_id/target_*/
# project_id). The UI keeps the full shape (it calls the service method
# directly). See docs/MCP_SERVER_CONTRACT.md.
_SLIM_RESOURCE_FIELDS = ("id", "association_role", "path", "kind", "size_bytes", "missing", "title")
_SLIM_STORAGE_FIELDS = (
    "id", "name", "version", "kind", "content_sha256", "size_bytes",
    "content_type", "status", "expires_at", "producing_run", "source_uri",
    "notes",
)
_PRIOR_RESOURCE_FIELDS = ("id", "association_role", "path", "association_attempt_index")
_SLIM_CLAIM_FIELDS = ("id", "statement", "confidence", "status", "scope")
_SLIM_REVIEW_FIELDS = ("id", "role", "verdict", "created_at", "synopsis", "findings", "notes", "evidence")


def slim_experiment_state(full: dict[str, Any]) -> dict[str, Any]:
    """Project a full get_state down to the agent-facing shape (detail, no waste)."""
    attempt = full.get("attempt_index")
    all_resources = full.get("resources", [])
    current = full.get("current_attempt_resources")
    if current is None:
        current = [r for r in all_resources if r.get("association_attempt_index") == attempt]
    prior = [r for r in all_resources if r.get("association_attempt_index") != attempt]

    slim: dict[str, Any] = {
        "id": full.get("id"),
        "name": full.get("name"),
        "status": full.get("status"),
        "attempt_index": attempt,
        "intent": full.get("intent"),
        "conclusion": full.get("conclusion"),
        "revision_context": full.get("revision_context"),
        "created_at": full.get("created_at"),
        "updated_at": full.get("updated_at"),
        "allowed_transitions": full.get("allowed_transitions", []),
        "gate_checklist": full.get("gate_checklist", {}),
        "mlflow_run": full.get("mlflow_run"),
        "claim_update_suggestions": full.get("claim_update_suggestions", []),
        "tested_claims": [
            {field: claim.get(field) for field in _SLIM_CLAIM_FIELDS}
            for claim in full.get("tested_claims", [])
        ],
        "current_attempt_resources": [
            {field: res.get(field) for field in _SLIM_RESOURCE_FIELDS}
            for res in current
        ],
        "storage_objects": [
            {field: obj.get(field) for field in _SLIM_STORAGE_FIELDS}
            for obj in full.get("storage_objects", [])
        ],
        "reviews": [
            {field: review.get(field) for field in _SLIM_REVIEW_FIELDS}
            for review in full.get("reviews", [])
        ],
    }
    # Only surface prior-attempt artifacts (as compact references) when a rerun
    # actually produced them — keeps single-attempt experiments lean.
    if prior:
        slim["prior_attempt_resources"] = [
            {field: res.get(field) for field in _PRIOR_RESOURCE_FIELDS}
            for res in prior
        ]
    return slim
