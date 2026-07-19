"""MLflow tracking integration.

This package contains the Merv facade around real MLflow services:
agent-facing tracking context, backend metric snapshots, and the local managed
server wrapper.
"""

from .exhibit import (
    METRICS_EXHIBIT_FILENAME,
    METRICS_EXHIBIT_KIND,
    build_metrics_exhibit,
    exhibit_bytes,
)
from .local_server import LocalMlflowServer
from .tracking import (
    MLFLOW_TERMINAL_RUN_STATUSES,
    CentralMlflowService,
    mlflow_experiment_name,
    mlflow_visible_for_status,
)

__all__ = [
    "METRICS_EXHIBIT_FILENAME",
    "METRICS_EXHIBIT_KIND",
    "MLFLOW_TERMINAL_RUN_STATUSES",
    "CentralMlflowService",
    "LocalMlflowServer",
    "build_metrics_exhibit",
    "exhibit_bytes",
    "mlflow_experiment_name",
    "mlflow_visible_for_status",
]
