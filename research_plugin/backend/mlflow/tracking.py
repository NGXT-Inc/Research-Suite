"""Centralized MLflow tracking context for experiment agents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

import httpx

from .metrics import snapshot_mlflow
from ..config import (
    resolve_mlflow_dashboard_url,
    resolve_mlflow_mode,
    resolve_mlflow_server_uri,
    resolve_mlflow_tracking_uri,
)


def mlflow_experiment_name(*, project_id: str, experiment_id: str) -> str:
    """Stable MLflow namespace for one Research Plugin experiment."""
    return f"rp/{project_id}/{experiment_id}"


MLFLOW_STATE_STATUSES = frozenset({"running", "experiment_review", "complete", "failed"})


def mlflow_visible_for_status(status: object) -> bool:
    """Whether experiment state should carry the MLflow context block."""
    return str(status or "") in MLFLOW_STATE_STATUSES


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
        self._control_uri = server_uri.strip().rstrip("/")
        self.server_uri = self._control_uri or self.tracking_uri
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

    def project_context(self, *, project_id: str) -> dict[str, object]:
        """Project-scoped MLflow navigation context for agents.

        This does not query MLflow. It gives agents the endpoint and namespace
        prefix they need to use MLflow's native APIs directly.
        """
        env: dict[str, str] = {"RP_PROJECT_ID": project_id}
        if self.tracking_uri:
            env["MLFLOW_TRACKING_URI"] = self.tracking_uri
        configured = bool(self.tracking_uri)
        result: dict[str, object] = {
            "configured": configured,
            "mode": self.mode or ("external" if configured else "unconfigured"),
            "tracking_uri": self.tracking_uri,
            "dashboard_url": self.dashboard_url,
            "project_id": project_id,
            "experiment_namespace_prefix": f"rp/{project_id}/",
            "env": env,
        }
        if not configured:
            result["note"] = self._unconfigured_note()
        return result

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
                else self._unconfigured_note()
            ),
        )

    def create_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        attempt_index: int = 1,
        run_name: str = "",
    ) -> dict[str, object]:
        """Best-effort control-plane creation of the initial MLflow run.

        The backend uses ``RESEARCH_PLUGIN_MLFLOW_SERVER_URI`` for this write.
        Agents still need ``RESEARCH_PLUGIN_MLFLOW_TRACKING_URI`` to resume the
        returned run id from their execution location, so both endpoints are
        required before the plugin creates a run.
        """
        context = self.context(project_id=project_id, experiment_id=experiment_id)
        name = run_name.strip() or f"{experiment_id}-attempt-{attempt_index}"
        result: dict[str, object] = {
            "created": False,
            "configured": context.configured,
            "control_configured": bool(self._control_uri),
            "experiment_name": context.experiment_name,
            "run_name": name,
        }
        if not context.configured:
            result["note"] = context.note
            return result
        if not self._control_uri:
            result["note"] = (
                "MLflow run creation requires RESEARCH_PLUGIN_MLFLOW_SERVER_URI "
                "so the control plane can create the run before handing its id "
                "to the agent."
            )
            return result
        try:
            start_ms = int(time.time() * 1000)
            with httpx.Client(timeout=3.0) as client:
                mlflow_experiment_id = self._ensure_mlflow_experiment(
                    client=client,
                    base=self._control_uri,
                    experiment_name=context.experiment_name,
                )
                payload = {
                    "experiment_id": mlflow_experiment_id,
                    "start_time": start_ms,
                    "run_name": name,
                    "tags": [
                        {"key": "project_id", "value": project_id},
                        {"key": "experiment_id", "value": experiment_id},
                        {"key": "attempt_index", "value": str(attempt_index)},
                        {"key": "created_by", "value": "research_plugin"},
                    ],
                }
                response = client.post(
                    f"{self._control_uri}/api/2.0/mlflow/runs/create",
                    json=payload,
                )
                response.raise_for_status()
                run = response.json().get("run") or {}
                info = run.get("info") or {}
        except Exception as exc:  # noqa: BLE001 - run creation is advisory
            result["error"] = f"MLflow run creation failed: {exc}"
            return result

        run_id = str(info.get("run_id") or info.get("run_uuid") or "")
        mlflow_experiment_id = str(info.get("experiment_id") or mlflow_experiment_id)
        result.update(
            {
                "created": bool(run_id),
                "experiment_id": mlflow_experiment_id,
                "run_id": run_id,
                "run_name": str(info.get("run_name") or name),
                "status": str(info.get("status") or "RUNNING"),
                "artifact_uri": str(info.get("artifact_uri") or ""),
                "created_at": _mlflow_ms_to_iso(info.get("start_time") or start_ms),
            }
        )
        if self.dashboard_url and mlflow_experiment_id and run_id:
            result["dashboard_run_url"] = (
                f"{self.dashboard_url}/#/experiments/{mlflow_experiment_id}/runs/{run_id}"
            )
        return result

    def _ensure_mlflow_experiment(
        self, *, client: httpx.Client, base: str, experiment_name: str
    ) -> str:
        found = self._find_mlflow_experiment_id(
            client=client,
            base=base,
            experiment_name=experiment_name,
        )
        if found:
            return found
        try:
            response = client.post(
                f"{base}/api/2.0/mlflow/experiments/create",
                json={"name": experiment_name},
            )
            response.raise_for_status()
            created = str(response.json().get("experiment_id") or "")
            if created:
                return created
        except httpx.HTTPError:
            # A concurrent creator may have won the race. Search once more
            # before letting the caller report the failure.
            pass
        found = self._find_mlflow_experiment_id(
            client=client,
            base=base,
            experiment_name=experiment_name,
        )
        if found:
            return found
        raise RuntimeError(f"MLflow experiment could not be created: {experiment_name}")

    def _find_mlflow_experiment_id(
        self, *, client: httpx.Client, base: str, experiment_name: str
    ) -> str:
        response = client.get(
            f"{base}/api/2.0/mlflow/experiments/search",
            params={
                "max_results": 1000,
                "filter": "name = '" + experiment_name.replace("'", "\\'") + "'",
            },
        )
        response.raise_for_status()
        for experiment in response.json().get("experiments") or []:
            if str(experiment.get("name") or "") == experiment_name:
                return str(experiment.get("experiment_id") or "")
        return ""

    def health(self) -> dict[str, object]:
        tracking_configured = bool(self.tracking_uri)
        read_configured = bool(self.server_uri)
        configured = tracking_configured or read_configured
        result: dict[str, object] = {
            "configured": configured,
            "tracking_configured": tracking_configured,
            "read_configured": read_configured,
            "mode": self.mode or ("external" if configured else "unconfigured"),
            "tracking_uri": self.tracking_uri,
            "dashboard_url": self.dashboard_url,
        }
        if read_configured:
            reachable = self._reachable()
            result["reachable"] = reachable
            if not reachable and not self.note:
                result["note"] = "MLflow is configured but not reachable."
        if read_configured and not tracking_configured and not result.get("note"):
            result["note"] = (
                "Backend MLflow reads are configured, but agents cannot log or "
                "browse with MLflow APIs until RESEARCH_PLUGIN_MLFLOW_TRACKING_URI "
                "is set to a run-reachable URL."
            )
        if self.server_uri and self.server_uri != self.tracking_uri:
            result["server_uri"] = self.server_uri
        if self.note:
            result["note"] = self.note
        return result

    def results_metrics(self, *, project_id: str, experiment_id: str) -> dict[str, object]:
        """Read experiment metrics from the centralized MLflow server."""
        context = self.context(project_id=project_id, experiment_id=experiment_id)
        read_uri = self.server_uri or context.tracking_uri
        if not read_uri:
            return {
                "experiment_id": experiment_id,
                "available": False,
                "source": "mlflow",
                "hint": context.note,
            }
        snapshot = snapshot_mlflow(
            read_uri,
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
        result = {
            "experiment_id": experiment_id,
            "available": True,
            **portable,
        }
        drill_url = self._dashboard_experiment_url(portable, context.experiment_name)
        if drill_url:
            result["dashboard_experiment_url"] = drill_url
        return result

    def _dashboard_experiment_url(
        self, snapshot: dict[str, object], experiment_name: str
    ) -> str:
        """Deep link into the MLflow UI for this experiment, if resolvable.

        The backend owns the namespace→URL mapping so UI surfaces never have to
        reconstruct MLflow's ``#/experiments/<numeric_id>`` route themselves.
        """
        if not self.dashboard_url:
            return ""
        experiments = snapshot.get("experiments") if isinstance(snapshot, dict) else None
        numeric_id = ""
        for entry in experiments or []:
            if str(entry.get("name") or "") == experiment_name:
                numeric_id = str(entry.get("experiment_id") or "")
                break
        if not numeric_id and experiments:
            numeric_id = str(experiments[0].get("experiment_id") or "")
        return f"{self.dashboard_url}/#/experiments/{numeric_id}" if numeric_id else ""

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

    def _unconfigured_note(self) -> str:
        if self.server_uri:
            return (
                "Backend MLflow reads are configured through "
                "RESEARCH_PLUGIN_MLFLOW_SERVER_URI, but agents cannot log or "
                "browse with MLflow APIs until RESEARCH_PLUGIN_MLFLOW_TRACKING_URI "
                "is set to a URL reachable from the run location."
            )
        return (
            self.note
            or "Centralized MLflow is not configured; set RESEARCH_PLUGIN_MLFLOW_TRACKING_URI."
        )


def _mlflow_ms_to_iso(value: object) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
