"""MLflow tracking integration.

This package contains the Research Plugin facade around real MLflow services:
agent-facing tracking context, backend metric snapshots, and the local managed
server wrapper.
"""

from .advisories import (
    ADVISORY_NOTE,
    advisory_fingerprint,
    detect_snapshot_advisories,
)
from .exhibit import (
    METRICS_EXHIBIT_FILENAME,
    METRICS_EXHIBIT_KIND,
    build_metrics_exhibit,
    exhibit_bytes,
    iso_to_epoch_ms,
)
from .local_server import LocalMlflowServer
from .tracking import (
    MLFLOW_TERMINAL_RUN_STATUSES,
    CentralMlflowService,
    mlflow_experiment_name,
    mlflow_visible_for_status,
)

__all__ = [
    "ADVISORY_NOTE",
    "METRICS_EXHIBIT_FILENAME",
    "METRICS_EXHIBIT_KIND",
    "MLFLOW_TERMINAL_RUN_STATUSES",
    "CentralMlflowService",
    "LocalMlflowServer",
    "advisory_fingerprint",
    "build_metrics_exhibit",
    "detect_snapshot_advisories",
    "exhibit_bytes",
    "iso_to_epoch_ms",
    "mlflow_experiment_name",
    "mlflow_visible_for_status",
]
