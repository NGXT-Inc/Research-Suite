"""Pure experiment projections owned by the application boundary."""

from __future__ import annotations

from typing import Any, Iterable, cast

from ...research_core.facade import ExperimentState
from ..gate_checklist import present_gate_checklist
from ..ports.storage import ProducedObject
from .claim_guidance import claim_update_suggestions


class SlimExperimentState(ExperimentState, total=False):
    """Agent-facing experiment detail: workflow substance without bookkeeping."""


_SLIM_ARTIFACT_FIELDS = (
    "id",
    "role",
    "path",
    "lens_id",
    "size_bytes",
    "title",
)
_SLIM_STORAGE_FIELDS = tuple(
    field
    for field in ProducedObject.__annotations__
    if field not in {"created_at", "updated_at", "last_accessed_at"}
)
_PRIOR_ARTIFACT_FIELDS = (
    "id",
    "role",
    "path",
    "attempt_index",
)
_SLIM_CLAIM_FIELDS = ("id", "statement", "confidence", "status", "scope")
_SLIM_REVIEW_FIELDS = (
    "id",
    "role",
    "verdict",
    "created_at",
    "synopsis",
    "findings",
    "notes",
    "evidence",
)


def project_fields(record: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: record.get(field) for field in fields}


def project_rows(
    records: Iterable[dict[str, Any]], fields: Iterable[str]
) -> list[dict[str, Any]]:
    fields = tuple(fields)
    return [project_fields(record, fields) for record in records]


def rich_experiment_state(
    full: ExperimentState,
    *,
    storage_objects: Iterable[ProducedObject | dict[str, Any]],
) -> ExperimentState:
    """Attach Storage facts without mutating Research's authoritative state.

    Existing rich JSON placed ``storage_objects`` immediately before
    ``mlflow_run``. Preserve that insertion order because cached HTTP bodies
    are serialized without key sorting.
    """

    result = dict(full)
    checklist = result.get("gate_checklist")
    if isinstance(checklist, dict):
        result["gate_checklist"] = present_gate_checklist(checklist)
    if "gate_checklist" in result and "claim_update_suggestions" not in result:
        items = list(result.items())
        index = list(result).index("gate_checklist") + 1
        items.insert(index, ("claim_update_suggestions", claim_update_suggestions(full)))
        result = dict(items)
    result.pop("storage_objects", None)
    items = list(result.items())
    storage_at = list(result).index("mlflow_run") if "mlflow_run" in result else len(items)
    items.insert(storage_at, ("storage_objects", list(storage_objects)))
    return cast(ExperimentState, dict(items))


def slim_experiment_state(
    full: ExperimentState,
    *,
    storage_objects: Iterable[ProducedObject | dict[str, Any]],
) -> SlimExperimentState:
    """Project rich experiment facts to the exact agent-facing wire shape."""

    rich = rich_experiment_state(full, storage_objects=storage_objects)
    attempt = rich.get("attempt_index")
    all_artifacts = rich.get("artifacts", [])
    current = rich.get("current_attempt_artifacts")
    if current is None:
        current = [
            artifact
            for artifact in all_artifacts
            if artifact.get("attempt_index") == attempt
        ]
    prior = [
        artifact
        for artifact in all_artifacts
        if artifact.get("attempt_index") != attempt
    ]

    slim: dict[str, Any] = {
        "id": rich.get("id"),
        "name": rich.get("name"),
        "status": rich.get("status"),
        "attempt_index": attempt,
        "intent": rich.get("intent"),
        "conclusion": rich.get("conclusion"),
        "revision_context": rich.get("revision_context"),
        "created_at": rich.get("created_at"),
        "updated_at": rich.get("updated_at"),
        "allowed_transitions": rich.get("allowed_transitions", []),
        "gate_checklist": rich.get("gate_checklist", {}),
        "mlflow_run": rich.get("mlflow_run"),
        "claim_update_suggestions": rich.get("claim_update_suggestions", []),
        "tested_claims": project_rows(rich.get("tested_claims", []), _SLIM_CLAIM_FIELDS),
        "current_attempt_artifacts": project_rows(current, _SLIM_ARTIFACT_FIELDS),
        "storage_objects": project_rows(rich.get("storage_objects", []), _SLIM_STORAGE_FIELDS),
        "reviews": project_rows(rich.get("reviews", []), _SLIM_REVIEW_FIELDS),
    }
    if prior:
        slim["prior_attempt_artifacts"] = project_rows(prior, _PRIOR_ARTIFACT_FIELDS)
    return cast(SlimExperimentState, slim)


__all__ = ["SlimExperimentState", "claim_update_suggestions", "project_fields", "project_rows",
           "rich_experiment_state", "slim_experiment_state"]
