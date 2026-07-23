"""JSON-safe value contracts at service-shaped component boundaries."""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
import re
import sqlite3
import unittest
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args, get_origin, get_type_hints, is_typeddict

from merv.brain.application.events import (
    DispatchResult,
    EventCatalogEntry,
    EventContext,
    EventReaction,
)
from merv.brain.application.experiments.tracking import (
    ExperimentDetailResponse,
    FinalizeTrackingResponse,
    TrackingContextResponse,
)
from merv.brain.application.experiments.presentation import SlimExperimentState
from merv.brain.application.experiments.transition import TransitionResponse
from merv.brain.application.ports.storage import ProducedObject
from merv.brain.application.ports.tracking import (
    CreateRunResult,
    FinalizeRunResult,
    MetricsSnapshot,
    TrackingCapabilities,
    TrackingContextPayload,
    TrackingExperimentSnapshot,
    TrackingMetric,
    TrackingRun,
    TrackingSnapshotRun,
)
from merv.brain.artifacts.facade import MetricFileSource
from merv.brain.artifacts.ports import (
    AssociatedEvidence,
    AssociationTarget,
    SubmittedDocument,
    SubmittedEvidence,
)
from merv.brain.kernel.events import StoredEvent, freeze_json_object
from merv.brain.kernel.ports.blob_store import (
    BlobDownloadTarget,
    BlobStat,
    BlobUploadTarget,
)
from merv.brain.kernel.ports.object_store import (
    DownloadTarget,
    ObjectStat,
    UploadPart,
    UploadTarget,
)
from merv.brain.kernel.ports.quota_admission import AdmissionRequest
from merv.brain.research_core.facade import (
    ExperimentCreateArgs,
    LiteratureSignal,
    ResearchSnapshot,
)
from merv.brain.research_core.gate_evaluation import (
    GateEvaluation,
    RequirementEvaluation,
)
from merv.brain.research_core.transition_types import (
    CommittedExperimentUpdate,
    ExhibitVerdict,
    ExperimentState,
    ExperimentSummary,
    PersistedRunState,
)
from tests.paths import BACKEND_ROOT


APPLICATION_DATACLASS_EXCLUSIONS = frozenset(
    {
        "merv.brain.application.experiments.create.CreateExperiment",
        "merv.brain.application.experiments.queries.ExperimentCollectionQuery",
        "merv.brain.application.experiments.reactions.ExperimentReactions",
        "merv.brain.application.experiments.tracking.AgentExperimentQuery",
        "merv.brain.application.experiments.tracking.ExperimentDetailQuery",
        "merv.brain.application.experiments.transition.TransitionExperiment",
        "merv.brain.application.reflections.ReflectionCommands",
        "merv.brain.application.reviews.ReadReviewStatus",
        "merv.brain.application.workflow.ProjectDashboardQuery",
        "merv.brain.application.workflow.StatusAndNextQuery",
    }
)


def _qualified(value: type) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _boundary_types() -> dict[str, type]:
    """Discover public DTOs in stable entrypoints and their value modules."""
    result: dict[str, type] = {}
    value_modules = {
        "application/events.py",
        "application/experiments/presentation.py",
        "kernel/events.py",
        "research_core/gate_evaluation.py",
        "research_core/transition_types.py",
    }
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        is_application_export = relative.startswith("application/") and "__all__" in source
        is_boundary_module = (
            relative.endswith("/facade.py")
            or "/ports/" in relative
            or relative in value_modules
        )
        if not (is_boundary_module or is_application_export):
            continue
        module_name = "merv.brain." + relative.removesuffix(".py").replace("/", ".")
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                name.startswith("_")
                or not isinstance(value, type)
                or value.__module__ != module.__name__
            ):
                continue
            if is_application_export and name not in getattr(module, "__all__", ()):
                continue
            qualified = _qualified(value)
            if (
                is_typeddict(value)
                or dataclasses.is_dataclass(value)
                and (
                    is_boundary_module
                    or qualified not in APPLICATION_DATACLASS_EXCLUSIONS
                )
            ):
                result[qualified] = value
    return result


EVENT = StoredEvent(
    id=7,
    project_id="proj_1",
    type="experiment.transitioned",
    target_type="experiment",
    target_id="exp_1",
    payload=freeze_json_object({"transition": "start_running", "steps": [1, 2]}),
    created_at="2026-07-21T12:00:00Z",
)
RUN: TrackingRun = {
    "run_id": "run_1",
    "run_name": "attempt-1",
    "status": "RUNNING",
    "artifact_uri": "s3://runs/1",
    "created_at": "2026-07-21T12:00:00Z",
    "created_by_plugin": True,
    "error": "",
}

# One non-empty sample per discovered value type. TypedDicts are ordinary dicts
# at runtime; including every declared field makes their nested shapes visible.
SAMPLES: dict[type, object] = {
    AssociatedEvidence: AssociatedEvidence(
        artifact_id="art_1",
        project_id="proj_1",
        role="report",
        attempt_index=2,
        lens_id="",
        path="experiments/example/report.md",
        title="Report",
        content_sha256="a" * 64,
        size_bytes=3,
        content_type="text/markdown",
        created_by="agent",
        created_at="2026-07-21T12:00:00Z",
        updated_at="2026-07-21T12:00:00Z",
        order=7,
    ),
    AssociationTarget: AssociationTarget("proj_1", 2),
    SubmittedDocument: SubmittedDocument(
        text="# Report",
        artifact_id="art_1",
        path="experiments/example/report.md",
        role="report",
        figure_links=("figures/result.png",),
    ),
    SubmittedEvidence: SubmittedEvidence(
        role="report",
        path="experiments/example/report.md",
        artifact_id="art_1",
        order=7,
        content="# Report",
    ),
    TrackingCapabilities: TrackingCapabilities(True, True, True),
    TrackingContextPayload: {
        "configured": True,
        "mode": "control",
        "tracking_uri": "https://tracking.example",
        "dashboard_url": "https://tracking.example/ui",
        "experiment_name": "proj_1.exp_1",
        "env": {"MLFLOW_TRACKING_URI": "https://tracking.example"},
        "note": "configured",
        "project_id": "proj_1",
        "experiment_namespace_prefix": "proj_1",
        "experiments": [{"id": "exp_1", "name": "Example"}],
    },
    TrackingRun: RUN,
    CreateRunResult: {"created": True, **RUN},
    FinalizeRunResult: {"run": RUN},
    TrackingMetric: {"last": 0.9, "step": 3, "min": 0.4, "max": 0.9},
    TrackingSnapshotRun: {
        "run_id": "run_1",
        "run_name": "attempt-1",
        "status": "RUNNING",
        "start_time": 1,
        "end_time": 2,
        "params": {"seed": 7},
        "tags": {"attempt": "1"},
        "metrics": {"accuracy": {"last": 0.9}},
        "metrics_capped_at": 50,
    },
    TrackingExperimentSnapshot: {"name": "proj_1.exp_1", "runs": [RUN]},
    MetricsSnapshot: {
        "available": True,
        "suspended": False,
        "experiments": [{"name": "proj_1.exp_1", "runs": []}],
    },
    ProducedObject: {
        "id": "so_1",
        "name": "models/checkpoint.bin",
        "version": 1,
        "kind": "model",
        "content_sha256": "c" * 64,
        "size_bytes": 12,
        "content_type": "application/octet-stream",
        "status": "available",
        "expires_at": None,
        "producing_run": "run_1",
        "source_uri": "",
        "notes": "retained",
        "created_at": "2026-07-21T12:00:00Z",
        "updated_at": "2026-07-21T12:00:00Z",
        "last_accessed_at": None,
    },
    MetricFileSource: {
        "path": "experiments/example/results.json",
        "artifact_id": "art_1",
        "sha256": "a" * 64,
        "submitted_at": "2026-07-21T12:00:00Z",
        "data": {"accuracy": 0.9},
    },
    TrackingContextResponse: {
        "project_id": "proj_1",
        "experiment_id": "exp_1",
        "scope": "experiment",
        "mlflow": {
            "configured": True,
            "mode": "control",
            "tracking_uri": "https://tracking.example",
            "dashboard_url": "https://tracking.example/ui",
            "experiment_name": "proj_1.exp_1",
            "env": {"MLFLOW_TRACKING_URI": "https://tracking.example"},
            "note": "configured",
            "project_id": "proj_1",
            "experiment_namespace_prefix": "proj_1",
            "experiments": [{"id": "exp_1", "name": "Example"}],
        },
        "guidance": "Log every run.",
    },
    FinalizeTrackingResponse: {
        "run": RUN,
        "project_id": "proj_1",
        "experiment_id": "exp_1",
        "experiment": {"id": "exp_1", "status": "completed"},
        "configured": True,
        "run_id": "run_1",
        "error": "",
        "feed_note": "Run finalized.",
    },
    ExperimentDetailResponse: {
        "id": "exp_1",
        "project_id": "proj_1",
        "name": "Example",
        "intent": "Test one claim",
        "status": "running",
        "attempt_index": 1,
        "mlflow_run": RUN,
        "mlflow": {"configured": True},
    },
    TransitionResponse: {
        "id": "exp_1",
        "project_id": "proj_1",
        "name": "Example",
        "intent": "Test one claim",
        "status": "running",
        "attempt_index": 1,
        "mlflow_run": RUN,
        "mlflow": {"configured": True},
        "mlflow_guidance": "Log every run.",
        "metrics_exhibit": {"pinned": True},
        "feed_note": "Experiment started.",
    },
    StoredEvent: EVENT,
    EventCatalogEntry: EventCatalogEntry(
        producer="merv.brain.research_core.experiments.ExperimentService.transition_with_event",
        event_type="experiment.transitioned",
        payload_version=1,
        transaction_boundary=(
            "merv.brain.research_core.experiments.ExperimentService."
            "transition_with_event"
        ),
        reaction_phase="post_commit",
        handler_identity="tracking_start",
        failure="fatal",
        idempotency="requires_adapter_key_for_redelivery",
    ),
    EventContext: EventContext(event=EVENT, state={"id": "exp_1"}),
    EventReaction: EventReaction(state={"id": "exp_1"}, value="noted"),
    DispatchResult: DispatchResult(
        state={"id": "exp_1"}, outcomes=MappingProxyType({"feed": "noted"})
    ),
    BlobStat: BlobStat("a" * 64, "proj_1", 2, "text/plain", "now", None),
    BlobDownloadTarget: {"url": "https://download.example"},
    BlobUploadTarget: {
        "upload_id": "upload_1",
        "url": "https://upload.example",
        "max_size_bytes": 10,
        "expires_at": None,
    },
    ObjectStat: ObjectStat("b" * 64, "proj_1", 3, "application/json", "now"),
    UploadPart: {"part_number": 1, "url": "https://upload.example/1"},
    UploadTarget: {
        "upload_id": "upload_1",
        "url": "https://upload.example",
        "parts": [{"part_number": 1, "url": "https://upload.example/1"}],
        "part_size": 5,
        "size_bytes": 5,
        "content_type": "application/json",
        "checksum_sha256": "b" * 64,
        "expires_in": 300,
    },
    DownloadTarget: {"url": "https://download.example"},
    AdmissionRequest: AdmissionRequest("tenant_1", 3600, 1.25),
    PersistedRunState: RUN,
    ExperimentCreateArgs: {
        "name": "example",
        "intent": "Test one claim",
        "tested_claim_ids": ["claim_1"],
        "claim_id": None,
        "claim_ids": None,
        "title": "",
        "hypothesis": "",
        "design": "",
        "success_criteria": "",
        "risks": "",
        "status": "planned",
        "project_id": "proj_1",
    },
    ExperimentState: {
        "id": "exp_1",
        "project_id": "proj_1",
        "name": "Example",
        "intent": "Test one claim",
        "status": "running",
        "attempt_index": 1,
        "mlflow_run": RUN,
    },
    ExperimentSummary: {
        "id": "exp_1",
        "project_id": "proj_1",
        "name": "Example",
        "intent": "Test one claim",
        "status": "running",
        "attempt_index": 1,
        "created_at": "2026-07-21T12:00:00Z",
        "updated_at": "2026-07-21T12:00:00Z",
    },
    SlimExperimentState: {
        "id": "exp_1",
        "project_id": "proj_1",
        "name": "Example",
        "intent": "Test one claim",
        "status": "running",
        "attempt_index": 1,
        "mlflow_run": RUN,
    },
    ExhibitVerdict: {
        "runs_found": 1,
        "result_files": 1,
        "attempt_index": 1,
        "mlflow": {"configured": True},
        "pinned": True,
    },
    CommittedExperimentUpdate: CommittedExperimentUpdate(
        state={"id": "exp_1", "status": "running"}, event=EVENT
    ),
    RequirementEvaluation: RequirementEvaluation(
        role="plan",
        status="valid",
        blocker_code="",
        enforcement_error="",
        problems=(),
        items=({"id": "resource:plan", "satisfied": True},),
    ),
    GateEvaluation: GateEvaluation(
        subject="experiment",
        status="planned",
        transition="submit_design",
        leads_to="design_review",
        terminal=False,
        requirements=(
            RequirementEvaluation(
                "plan", "valid", "", "", (), ({"id": "resource:plan"},)
            ),
        ),
        review=None,
        legal_transitions=(
            {"transition": "submit_design", "leads_to": "design_review"},
        ),
    ),
    ResearchSnapshot: ResearchSnapshot(
        project_id="proj_1",
        requested_experiment_id="exp_1",
        project={"id": "proj_1"},
        claims=[{"id": "clm_1"}],
        experiments=[{"id": "exp_1"}],
        experiment_states=[{"id": "exp_1", "status": "running"}],
        selected_experiment={"id": "exp_1"},
        open_reflection=None,
        latest_published_reflection=None,
        reflection_signal={"needed": False},
        gate_evaluations={
            "exp_1": GateEvaluation(
                "experiment",
                "running",
                "submit_results",
                "experiment_review",
                False,
                (),
                None,
                ({"transition": "submit_results", "leads_to": "experiment_review"},),
            )
        },
        recent_claims=[{"id": "clm_1"}],
        claim_events_since_reflection=[],
        literature_signal=LiteratureSignal(papers_total=1, papers_unreviewed=0),
    ),
    LiteratureSignal: LiteratureSignal(papers_total=1, papers_unreviewed=0),
}


JSON_ROUNDTRIP_DEBT: Counter[tuple[str, str]] = Counter()

ANNOTATION_DEBT = frozenset(
    {
        ("merv.brain.application.ports.tracking.TrackingMetric.step", "object"),
        ("merv.brain.application.ports.tracking.TrackingSnapshotRun.params", "object"),
        ("merv.brain.artifacts.facade.MetricFileSource.data", "object"),
        (
            "merv.brain.application.experiments.tracking.ExperimentDetailResponse.mlflow",
            "Any",
        ),
        (
            "merv.brain.application.experiments.transition.TransitionResponse.metrics_exhibit",
            "object",
        ),
        ("merv.brain.application.events.EventReaction.value", "object"),
        ("merv.brain.application.events.DispatchResult.outcomes", "object"),
        ("merv.brain.research_core.facade.ResearchSnapshot.project", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.claims", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.experiments", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.experiment_states", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.selected_experiment", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.open_reflection", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.latest_published_reflection", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.reflection_signal", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.recent_claims", "Any"),
        ("merv.brain.research_core.facade.ResearchSnapshot.claim_events_since_reflection", "Any"),
        ("merv.brain.research_core.transition_types.ExhibitVerdict.mlflow", "object"),
    }
)

_PERSISTENCE_OR_SERVICE = re.compile(
    r"(?:Connection|Cursor|Row|BaseStateStore|StateStore|Repository|Service|Facade)$"
)


def _to_json_value(value: object, *, boundary_types: set[type]) -> object:
    value_type = type(value)
    if _PERSISTENCE_OR_SERVICE.search(value_type.__name__):
        raise TypeError(f"boundary contains runtime service: {_qualified(value_type)}")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if value_type not in boundary_types:
            raise TypeError(f"unregistered boundary dataclass: {_qualified(value_type)}")
        return {
            field.name: _to_json_value(getattr(value, field.name), boundary_types=boundary_types)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        converted: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON mapping key is {type(key).__name__}, not str")
            converted[key] = _to_json_value(item, boundary_types=boundary_types)
        return converted
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item, boundary_types=boundary_types) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"boundary contains non-JSON value: {_qualified(value_type)}")


def _annotation_nodes(annotation: object):
    yield annotation
    for argument in get_args(annotation):
        yield from _annotation_nodes(argument)


class BoundaryValueContractTest(unittest.TestCase):
    def test_public_boundary_values_have_complete_representative_samples(self) -> None:
        discovered = _boundary_types()
        sampled = {_qualified(value_type) for value_type in SAMPLES}
        self.assertEqual(sampled, set(discovered))
        self.assertFalse(APPLICATION_DATACLASS_EXCLUSIONS & set(discovered))
        for qualified in APPLICATION_DATACLASS_EXCLUSIONS:
            module_name, class_name = qualified.rsplit(".", 1)
            module = importlib.import_module(module_name)
            value = getattr(module, class_name, None)
            self.assertTrue(dataclasses.is_dataclass(value), qualified)
            self.assertIn(class_name, module.__all__, qualified)
        for value_type, sample in SAMPLES.items():
            if is_typeddict(value_type):
                with self.subTest(value_type=_qualified(value_type)):
                    self.assertEqual(set(sample), set(get_type_hints(value_type)))

    def test_boundary_samples_are_json_roundtrippable_except_exact_debt(self) -> None:
        boundary_types = set(_boundary_types().values())
        failures: Counter[tuple[str, str]] = Counter()
        for value_type, sample in SAMPLES.items():
            with self.subTest(value_type=_qualified(value_type)):
                try:
                    normalized = _to_json_value(sample, boundary_types=boundary_types)
                    encoded = json.dumps(normalized, allow_nan=False, sort_keys=True)
                    self.assertEqual(json.loads(encoded), normalized)
                except TypeError as exc:
                    failures[(_qualified(value_type), str(exc))] += 1
        self.assertEqual(failures, JSON_ROUNDTRIP_DEBT)

    def test_boundary_annotation_debt_is_exact_and_has_no_persistence_types(self) -> None:
        debt: set[tuple[str, str]] = set()
        forbidden: list[str] = []
        for value_type in _boundary_types().values():
            for field, annotation in get_type_hints(value_type).items():
                label = f"{_qualified(value_type)}.{field}"
                nodes = tuple(_annotation_nodes(annotation))
                if Any in nodes:
                    debt.add((label, "Any"))
                if object in nodes:
                    debt.add((label, "object"))
                origin = get_origin(annotation)
                args = get_args(annotation)
                if origin is dict and args and args[0] is not str:
                    debt.add((label, "non-string-key"))
                for node in nodes:
                    if isinstance(node, type) and _PERSISTENCE_OR_SERVICE.search(node.__name__):
                        forbidden.append(f"{label}: {_qualified(node)}")
        self.assertFalse(forbidden, "persistence/service types escaped: " + ", ".join(forbidden))
        self.assertEqual(debt, ANNOTATION_DEBT)

    def test_runtime_connection_or_service_objects_are_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaisesRegex(TypeError, "runtime service"):
            _to_json_value(connection, boundary_types=set(_boundary_types().values()))
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT 1 AS value").fetchone()
        with self.assertRaisesRegex(TypeError, "runtime service"):
            _to_json_value(row, boundary_types=set(_boundary_types().values()))


if __name__ == "__main__":
    unittest.main()
