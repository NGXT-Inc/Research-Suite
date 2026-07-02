"""MLflow tracking integration.

This package contains the Research Plugin facade around real MLflow services:
agent-facing tracking context, backend metric snapshots, and the local managed
server wrapper.
"""

from .local_server import LocalMlflowServer
from .tracking import (
    MLFLOW_TERMINAL_RUN_STATUSES,
    CentralMlflowService,
    mlflow_experiment_name,
    mlflow_visible_for_status,
)

__all__ = [
    "MLFLOW_TERMINAL_RUN_STATUSES",
    "CentralMlflowService",
    "LocalMlflowServer",
    "mlflow_experiment_name",
    "mlflow_visible_for_status",
]
