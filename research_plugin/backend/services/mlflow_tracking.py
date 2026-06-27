"""Centralized MLflow tracking context for experiment agents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

import httpx

from ..mlflow_metrics import snapshot_mlflow
from ..config import (
    resolve_mlflow_dashboard_url,
    resolve_mlflow_mode,
    resolve_mlflow_server_uri,
    resolve_mlflow_tracking_uri,
)


def mlflow_experiment_name(*, project_id: str, experiment_id: str) -> str:
    """Stable MLflow namespace for one Research Plugin experiment."""
    return f"rp/{project_id}/{experiment_id}"


@dataclass(frozen=True)
class MlflowTrackingContext:
    """The agent-facing MLflow block for one experiment."""

    configured: bool
    mode: str
    tracking_uri: str
    dashboard_url: str
    experiment_name: str
    env: dict[str, str]
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "configured": self.configured,
            "mode": self.mode,
            "tracking_uri": self.tracking_uri,
            "dashboard_url": self.dashboard_url,
            "experiment_name": self.experiment_name,
            "env": dict(self.env),
        }
        if self.note:
            result["note"] = self.note
        return result


class CentralMlflowService:
    """Backend-owned MLflow naming and endpoint policy.

    This service intentionally does not inspect sandbox providers. It answers
    the only question agents should need: which MLflow endpoint and namespace
    should this quantitative run use?
    """

    def __init__(
        self,
        *,
        mode: str = "",
        tracking_uri: str = "",
        server_uri: str = "",
        dashboard_url: str = "",
        note: str = "",
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        self.mode = mode.strip().lower()
        self.tracking_uri = tracking_uri.strip().rstrip("/")
        self.server_uri = server_uri.strip().rstrip("/") or self.tracking_uri
        self.dashboard_url = (dashboard_url.strip().rstrip("/") or self.tracking_uri)
        self.note = note.strip()
        self._health_check = health_check
        self._last_probe_at = 0.0
        self._last_reachable: bool | None = None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "CentralMlflowService":
        return cls(
            mode=resolve_mlflow_mode(env),
            tracking_uri=resolve_mlflow_tracking_uri(env),
            server_uri=resolve_mlflow_server_uri(env),
            dashboard_url=resolve_mlflow_dashboard_url(env),
        )

    def context(
        self,
        *,
        project_id: str,
        experiment_id: str,
        attempt_id: str = "",
        sandbox_id: str = "",
        execution_backend: str = "",
    ) -> MlflowTrackingContext:
        experiment_name = mlflow_experiment_name(
            project_id=project_id, experiment_id=experiment_id
        )
        env: dict[str, str] = {
            "MLFLOW_EXPERIMENT_NAME": experiment_name,
            "RP_PROJECT_ID": project_id,
            "RP_EXPERIMENT_ID": experiment_id,
        }
        if self.tracking_uri:
            env["MLFLOW_TRACKING_URI"] = self.tracking_uri
        if attempt_id:
            env["RP_ATTEMPT_ID"] = attempt_id
        if sandbox_id:
            env["RP_SANDBOX_ID"] = sandbox_id
        if execution_backend:
            env["RP_EXECUTION_BACKEND"] = execution_backend
        configured = bool(self.tracking_uri)
        return MlflowTrackingContext(
            configured=configured,
            mode=self.mode or ("external" if configured else "unconfigured"),
            tracking_uri=self.tracking_uri,
            dashboard_url=self.dashboard_url,
            experiment_name=experiment_name,
            env=env,
            note=(
                ""
                if configured
                else self.note
                or "Centralized MLflow is not configured; set RESEARCH_PLUGIN_MLFLOW_TRACKING_URI."
            ),
        )

    def health(self) -> dict[str, object]:
        configured = bool(self.tracking_uri)
        result: dict[str, object] = {
            "configured": configured,
            "mode": self.mode or ("external" if configured else "unconfigured"),
            "tracking_uri": self.tracking_uri,
            "dashboard_url": self.dashboard_url,
        }
        if configured:
            reachable = self._reachable()
            result["reachable"] = reachable
            if not reachable and not self.note:
                result["note"] = "MLflow is configured but not reachable."
        if self.server_uri and self.server_uri != self.tracking_uri:
            result["server_uri"] = self.server_uri
        if self.note:
            result["note"] = self.note
        return result

    def results_metrics(self, *, project_id: str, experiment_id: str) -> dict[str, object]:
        """Read experiment metrics from the centralized MLflow server."""
        context = self.context(project_id=project_id, experiment_id=experiment_id)
        if not context.configured:
            return {
                "experiment_id": experiment_id,
                "available": False,
                "source": "mlflow",
                "hint": context.note,
            }
        snapshot = snapshot_mlflow(
            self.server_uri or context.tracking_uri,
            experiment_name=context.experiment_name,
        )
        if not isinstance(snapshot, dict):
            return {
                "experiment_id": experiment_id,
                "available": False,
                "source": "mlflow",
                "hint": "No MLflow runs found for this experiment yet.",
            }
        portable = dict(snapshot)
        portable.pop("base_url", None)
        return {
            "experiment_id": experiment_id,
            "available": True,
            **portable,
        }

    def _reachable(self) -> bool:
        if self._health_check is not None:
            return bool(self._health_check())
        now = time.monotonic()
        if self._last_reachable is not None and now - self._last_probe_at < 5.0:
            return self._last_reachable
        self._last_probe_at = now
        try:
            response = httpx.get(f"{self.server_uri}/health", timeout=0.5)
            self._last_reachable = response.status_code < 500
        except httpx.HTTPError:
            self._last_reachable = False
        return self._last_reachable
