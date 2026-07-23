"""Application-owned contract for experiment tracking.

These types describe only the tracking behavior used by experiment workflows.
They are internal structural contracts, not new public wire models.  Concrete
tracking products live outside this package and implement ``ExperimentTracking``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol, TypedDict, runtime_checkable


# The application deliberately bounds a metrics exhibit rather than treating
# an external tracking service as an unlimited archive mirror.
MAX_TRACKING_SNAPSHOT_RUNS: Final = 50

# The tracking contract fixes the external namespace and normalized run-status
# vocabulary used on both sides of the port.  Concrete adapters own how those
# values are sent to and read from their tracking product.
TRACKING_NAMESPACE_PREFIX: Final = "merv"
TRACKING_TERMINAL_RUN_STATUSES: Final = frozenset(
    {"FINISHED", "FAILED", "KILLED"}
)


def tracking_experiment_name(*, project_id: str, experiment_id: str) -> str:
    """Stable external-tracking namespace for one Merv experiment."""
    return f"{TRACKING_NAMESPACE_PREFIX}/{project_id}/{experiment_id}"


@dataclass(frozen=True, slots=True)
class TrackingCapabilities:
    """Independent configuration facts exposed by a tracking adapter.

    ``logging`` means an execution agent can log to the advertised endpoint;
    ``control`` means the backend can create or update runs; and ``readback``
    means the backend can query runs for exhibits.  Keeping these separate is
    important because a backend-only endpoint supports control and readback
    without being reachable from an execution environment.
    """

    logging: bool
    control: bool
    readback: bool


TRACKING_CAPABILITY_TRUTH_TABLE: Final = MappingProxyType(
    {
        (logging, control): TrackingCapabilities(
            logging=logging, control=control, readback=logging or control
        )
        for logging in (False, True)
        for control in (False, True)
    }
)


def capabilities_for_configuration(
    *, logging: bool, control: bool
) -> TrackingCapabilities:
    """Resolve the explicit logging/control configuration truth table."""
    return TRACKING_CAPABILITY_TRUTH_TABLE[(bool(logging), bool(control))]


class TrackingContextPayload(TypedDict, total=False):
    configured: bool
    mode: str
    tracking_uri: str
    dashboard_url: str
    experiment_name: str
    env: dict[str, str]
    note: str
    project_id: str
    experiment_namespace_prefix: str
    experiments: list[dict[str, str]]


@runtime_checkable
class TrackingContext(Protocol):
    def to_dict(self) -> TrackingContextPayload: ...


class TrackingRun(TypedDict, total=False):
    run_id: str
    run_name: str
    status: str
    artifact_uri: str
    created_at: str
    created_by_plugin: bool
    error: str


class CreateRunResult(TypedDict, total=False):
    created: bool
    run_id: str
    run_name: str
    status: str
    artifact_uri: str
    created_at: str
    created_by_plugin: bool
    error: str


class FinalizeRunResult(TypedDict, total=False):
    run: TrackingRun


class TrackingMetric(TypedDict, total=False):
    last: float | None
    step: object
    min: float
    max: float


class TrackingSnapshotRun(TypedDict, total=False):
    run_id: str
    run_name: str
    status: str
    start_time: int
    end_time: int
    params: dict[str, object]
    tags: dict[str, str]
    metrics: dict[str, TrackingMetric]
    metrics_capped_at: int


class TrackingExperimentSnapshot(TypedDict, total=False):
    name: str
    runs: list[TrackingSnapshotRun]


class MetricsSnapshot(TypedDict, total=False):
    available: bool
    suspended: bool
    experiments: list[TrackingExperimentSnapshot]


@runtime_checkable
class ExperimentTracking(Protocol):
    """Small command/readback port needed by experiment workflows."""

    def capabilities(self) -> TrackingCapabilities: ...

    def context(
        self,
        *,
        project_id: str,
        experiment_id: str,
        include_credentials: bool = False,
    ) -> TrackingContext: ...

    def project_context(
        self, *, project_id: str, include_credentials: bool = False
    ) -> TrackingContextPayload: ...

    def create_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        attempt_index: int,
        run_name: str,
    ) -> CreateRunResult: ...

    def finalize_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run_id: str,
        status: str | None,
        wait_seconds: float,
    ) -> FinalizeRunResult:
        """Repeated calls for the same run and status must be safe."""
        ...

    def results_metrics(
        self, *, project_id: str, experiment_id: str
    ) -> MetricsSnapshot: ...


__all__ = [
    "CreateRunResult",
    "ExperimentTracking",
    "FinalizeRunResult",
    "MAX_TRACKING_SNAPSHOT_RUNS",
    "MetricsSnapshot",
    "TRACKING_NAMESPACE_PREFIX",
    "TRACKING_TERMINAL_RUN_STATUSES",
    "TRACKING_CAPABILITY_TRUTH_TABLE",
    "TrackingCapabilities",
    "TrackingContext",
    "TrackingContextPayload",
    "TrackingExperimentSnapshot",
    "TrackingMetric",
    "TrackingRun",
    "TrackingSnapshotRun",
    "capabilities_for_configuration",
    "tracking_experiment_name",
]
