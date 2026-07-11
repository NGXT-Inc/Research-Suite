"""Metrics-exhibit orchestration for the tool surface.

The surface layer composes what the pure builder cannot reach across module
boundaries: MLflow readback (mlflow), the attempt window (research_core
events), and pinned result-file sources (artifacts). ``experiment.exhibit``
previews the exhibit during ``running``; the ``submit_results`` tool path
calls the same generation, then pins the bytes as a system-authored resource
— runs logged after that moment remain in MLflow but are outside the finalized
attempt exhibit.
"""

from __future__ import annotations

from typing import Any

from ..domain.paths import experiment_folder_rel
from ..mlflow import (
    ADVISORY_NOTE,
    METRICS_EXHIBIT_FILENAME,
    build_metrics_exhibit,
    detect_snapshot_advisories,
    exhibit_bytes,
    iso_to_epoch_ms,
    mlflow_experiment_name,
)
from ..artifacts.roles import EXHIBIT_ROLE
from ..utils import WorkflowError


def exhibit_rel_path(*, experiment_id: str, name: str) -> str:
    """Canonical repo-relative exhibit path inside the experiment folder."""
    folder = experiment_folder_rel(experiment_id=experiment_id, name=name)
    return f"{folder}{METRICS_EXHIBIT_FILENAME}"


def generate_metrics_exhibit(
    *,
    experiments: Any,
    resources: Any,
    mlflow_tracking: Any,
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One exhibit generation from current state — shared verbatim by the
    preview tool and the submit_results finalize, so preview == final for
    identical state. Returns (exhibit, snapshot); the snapshot rides along so
    callers can run advisory detection without a second MLflow read."""
    project_id = str(state.get("project_id") or "")
    experiment_id = str(state.get("id") or "")
    attempt_index = int(state.get("attempt_index") or 1)
    snapshot = None
    configured = False
    if mlflow_tracking is not None:
        configured = bool(
            getattr(mlflow_tracking, "server_uri", "")
            or getattr(mlflow_tracking, "tracking_uri", "")
        )
        if configured:
            snapshot = mlflow_tracking.results_metrics(
                project_id=project_id, experiment_id=experiment_id
            )
    exhibit = build_metrics_exhibit(
        project_id=project_id,
        experiment_id=experiment_id,
        attempt_index=attempt_index,
        experiment_name=mlflow_experiment_name(
            project_id=project_id, experiment_id=experiment_id
        ),
        window_started_at=experiments.attempt_started_running_at(
            experiment_id=experiment_id
        ),
        snapshot=snapshot,
        mlflow_configured=configured,
        file_sources=resources.metric_file_sources(
            target_id=experiment_id, attempt_index=attempt_index
        ),
    )
    return exhibit, snapshot


def observe_exhibit_advisories(
    *,
    experiments: Any,
    state: dict[str, Any],
    exhibit: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Detect advisories on the attempt-window runs and record the
    observation on the experiment (deduped events + latest stored set).
    An unavailable snapshot records nothing — an unreachable MLflow is not
    evidence that a previously seen problem cleared."""
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return []
    advisories = detect_snapshot_advisories(
        snapshot,
        window_started_ms=iso_to_epoch_ms(exhibit["window"]["started_at"] or None),
    )
    experiments.note_mlflow_advisories(
        experiment_id=str(state.get("id") or ""),
        project_id=str(state.get("project_id") or "") or None,
        advisories=advisories,
    )
    return advisories


def _should_pin(*, exhibit: dict[str, Any], state: dict[str, Any]) -> bool:
    """Attempt-window runs trigger the exhibit; result files only enrich one.
    Experiments with no runs (qualitative work) get no exhibit and no gate
    machinery. The unreachable-MLflow clause keeps the record honest: a
    plugin-created run identity proves this attempt logged quantitatively, so
    an outage at submit time pins a visibly unavailable exhibit rather than
    silently skipping the record."""
    if exhibit["verdict"]["runs_found"]:
        return True
    mlflow_block = exhibit["mlflow"]
    run = state.get("mlflow_run") or {}
    return bool(
        mlflow_block["configured"] and not mlflow_block["available"] and run.get("run_id")
    )


def finalize_metrics_exhibit(
    *,
    experiments: Any,
    resources: Any,
    mlflow_tracking: Any,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Generate the authoritative exhibit for a submit_results attempt, pin it
    as a system-authored resource on the current attempt, and record the
    generation verdict. Returns the exhibit, or None when the attempt has
    nothing quantitative to exhibit (no gate machinery for those)."""
    exhibit, snapshot = generate_metrics_exhibit(
        experiments=experiments,
        resources=resources,
        mlflow_tracking=mlflow_tracking,
        state=state,
    )
    # The finalize read is the attempt's last observation — record what it
    # saw so the review has the advisory trail beside the pinned numbers.
    observe_exhibit_advisories(
        experiments=experiments, state=state, exhibit=exhibit, snapshot=snapshot
    )
    project_id = str(state.get("project_id") or "")
    experiment_id = str(state.get("id") or "")
    pinned = _should_pin(exhibit=exhibit, state=state)
    experiments.record_exhibit_verdict(
        experiment_id=experiment_id,
        project_id=project_id,
        verdict={
            **exhibit["verdict"],
            "attempt_index": exhibit["attempt_index"],
            "mlflow": exhibit["mlflow"],
            "pinned": pinned,
        },
    )
    if not pinned:
        return None
    resources.pin_system_artifact(
        path=exhibit_rel_path(
            experiment_id=experiment_id, name=str(state.get("name") or "")
        ),
        target_type="experiment",
        target_id=experiment_id,
        role=EXHIBIT_ROLE,
        content_bytes=exhibit_bytes(exhibit),
        content_type="application/json",
        title="Metrics exhibit (system-generated)",
        kind="result",
        project_id=project_id,
    )
    return exhibit


def preview_metrics_exhibit(
    *,
    experiments: Any,
    resources: Any,
    mlflow_tracking: Any,
    experiment_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Read-only exhibit preview while the experiment is running, so the
    report can be written around the record before submit_results pins it."""
    state = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
    if str(state.get("status")) != "running":
        raise WorkflowError(
            f"experiment.exhibit previews a running experiment; this one is "
            f"{state.get('status')!r}. After submit_results, read the pinned "
            "exhibit resource instead (resource.find)."
        )
    exhibit, snapshot = generate_metrics_exhibit(
        experiments=experiments,
        resources=resources,
        mlflow_tracking=mlflow_tracking,
        state=state,
    )
    advisories = observe_exhibit_advisories(
        experiments=experiments, state=state, exhibit=exhibit, snapshot=snapshot
    )
    path = exhibit_rel_path(
        experiment_id=str(state.get("id") or experiment_id),
        name=str(state.get("name") or ""),
    )
    result: dict[str, Any] = {
        "project_id": str(state.get("project_id") or ""),
        "experiment_id": experiment_id,
        "exhibit_path": path,
        "exhibit": exhibit,
        "guidance": (
            "Preview of the system-generated metrics exhibit. At "
            "submit_results the system regenerates it from the same sources "
            f"and pins it at {path} when matching runs are found, or when "
            "MLflow is unavailable after a plugin-created run. The "
            "newest 50 runs are captured without curation and the exhibit "
            "records when that cap is reached. Later runs remain in MLflow "
            "but are outside the finalized exhibit. When pinned, report.md "
            f"must reference {METRICS_EXHIBIT_FILENAME} and interpret it "
            "rather than restate numbers by hand."
        ),
    }
    if advisories:
        result["advisories"] = advisories
        result["advisory_note"] = ADVISORY_NOTE
    return result
