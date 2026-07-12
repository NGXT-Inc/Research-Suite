"""Centralized MLflow tracking context for experiment agents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .metrics import (
    MlflowSnapshotError,
    search_mlflow_experiments,
    snapshot_mlflow,
)
from .config import (
    MLFLOW_AGENT_USERNAME,
    resolve_mlflow_agent_key,
    resolve_mlflow_dashboard_url,
    resolve_mlflow_mode,
    resolve_mlflow_server_uri,
    resolve_mlflow_tracking_uri,
)


def mlflow_experiment_name(*, project_id: str, experiment_id: str) -> str:
    """Stable MLflow namespace for one Merv experiment."""
    return f"rp/{project_id}/{experiment_id}"


MLFLOW_STATE_STATUSES = frozenset({"running", "experiment_review", "complete", "failed"})
MLFLOW_TERMINAL_RUN_STATUSES = frozenset({"FINISHED", "FAILED", "KILLED"})


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
        agent_key: str = "",
        health_check: Callable[[], bool] | None = None,
    ) -> None:
        self.mode = mode.strip().lower()
        self.tracking_uri = tracking_uri.strip().rstrip("/")
        self._control_uri = server_uri.strip().rstrip("/")
        self.server_uri = self._control_uri or self.tracking_uri
        self.dashboard_url = (dashboard_url.strip().rstrip("/") or self.tracking_uri)
        self.note = note.strip()
        # Credential for the authenticated hosted /mlflow route. Only ever
        # serialized into agent-facing env blocks (include_credentials=True);
        # UI-facing views keep the default and never see it.
        self.agent_key = agent_key.strip()
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
            agent_key=resolve_mlflow_agent_key(env),
        )

    def _credential_env(self, *, include_credentials: bool) -> dict[str, str]:
        # MLflow's client turns this pair into Basic auth on every tracking
        # and artifact call; the same pair answers the browser's 401 prompt.
        if not (include_credentials and self.agent_key and self.tracking_uri):
            return {}
        return {
            "MLFLOW_TRACKING_USERNAME": MLFLOW_AGENT_USERNAME,
            "MLFLOW_TRACKING_PASSWORD": self.agent_key,
        }

    def project_context(
        self, *, project_id: str, include_credentials: bool = False
    ) -> dict[str, object]:
        """Project-scoped MLflow navigation context for agents.

        This does not query MLflow. It gives agents the endpoint and namespace
        prefix they need to use MLflow's native APIs directly.
        """
        env: dict[str, str] = {"RP_PROJECT_ID": project_id}
        if self.tracking_uri:
            env["MLFLOW_TRACKING_URI"] = self.tracking_uri
        env.update(self._credential_env(include_credentials=include_credentials))
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
        include_credentials: bool = False,
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
        env.update(self._credential_env(include_credentials=include_credentials))
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
            result["error"] = f"MLflow run creation failed: {self._redacted(exc)}"
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

    def finalize_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run_id: str,
        status: str | None = "FINISHED",
        wait_seconds: float = 2.0,
    ) -> dict[str, object]:
        """Finalize a run through MLflow REST, then read it back.

        This gives agents one canonical post-execution call: set the terminal
        status when the backend write URI is configured, then poll the MLflow
        read API briefly so stale immediate ``RUNNING`` readbacks do not leak
        into plugin state.
        """
        context = self.context(project_id=project_id, experiment_id=experiment_id)
        run_id = run_id.strip()
        normalized_status = str(status or "").strip().upper()
        result: dict[str, object] = {
            "configured": bool(self.server_uri or self.tracking_uri),
            "control_configured": bool(self._control_uri),
            "experiment_name": context.experiment_name,
            "run_id": run_id,
            "requested_status": normalized_status or None,
        }
        if not run_id:
            result["error"] = "MLflow run id is required."
            return result
        if normalized_status and normalized_status not in MLFLOW_TERMINAL_RUN_STATUSES:
            result["error"] = (
                "MLflow terminal status must be one of: "
                + ", ".join(sorted(MLFLOW_TERMINAL_RUN_STATUSES))
            )
            return result
        read_uri = self.server_uri or self.tracking_uri
        if not read_uri:
            result["note"] = self._unconfigured_note()
            return result

        update_attempted = bool(normalized_status and self._control_uri)
        result["update"] = {
            "attempted": update_attempted,
            "status": normalized_status or None,
            "applied": False,
        }
        if normalized_status and not self._control_uri:
            result["update"] = {
                "attempted": False,
                "status": normalized_status,
                "applied": False,
                "note": (
                    "MLflow run status update requires "
                    "RESEARCH_PLUGIN_MLFLOW_SERVER_URI; readback only."
                ),
            }

        wait = max(0.0, min(float(wait_seconds or 0.0), 10.0))
        deadline = time.monotonic() + wait
        attempts = 0
        try:
            with httpx.Client(timeout=3.0) as client:
                if update_attempted:
                    # Read before write: the training script is usually the
                    # primary writer, so a run that is already terminal keeps
                    # its status (e.g. a script-recorded FAILED must not be
                    # rewritten by our FINISHED default).
                    pre_status = str(
                        self._read_run(
                            client=client, base=read_uri, run_id=run_id
                        ).get("status")
                        or ""
                    )
                    if pre_status in MLFLOW_TERMINAL_RUN_STATUSES:
                        result["update"] = {
                            "attempted": False,
                            "status": normalized_status,
                            "applied": False,
                            "skipped_already_terminal": pre_status,
                            "note": (
                                f"run is already {pre_status}; refusing to "
                                "overwrite a terminal status — readback only"
                            ),
                        }
                    else:
                        try:
                            response = client.post(
                                f"{self._control_uri}/api/2.0/mlflow/runs/update",
                                json={
                                    "run_id": run_id,
                                    "status": normalized_status,
                                    "end_time": int(time.time() * 1000),
                                },
                            )
                            response.raise_for_status()
                            result["update"] = {
                                "attempted": True,
                                "status": normalized_status,
                                "applied": True,
                            }
                        except Exception as exc:  # noqa: BLE001
                            result["update"] = {
                                "attempted": True,
                                "status": normalized_status,
                                "applied": False,
                                "error": (
                                    "MLflow run status update failed: "
                                    f"{self._redacted(exc)}"
                                ),
                            }
                run: dict[str, object] = {}
                # Poll until the run reads back terminal or the deadline hits —
                # in readback-only mode (status=null) just as much as after an
                # update, since the stale immediate RUNNING readback is exactly
                # what this helper exists to absorb.
                while True:
                    attempts += 1
                    run = self._read_run(client=client, base=read_uri, run_id=run_id)
                    status_seen = str(run.get("status") or "")
                    if (
                        status_seen in MLFLOW_TERMINAL_RUN_STATUSES
                        or time.monotonic() >= deadline
                    ):
                        break
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        except Exception as exc:  # noqa: BLE001 - readback is advisory
            result["error"] = f"MLflow run finalize/readback failed: {self._redacted(exc)}"
            result["readback_attempts"] = attempts
            return result

        result["readback_attempts"] = attempts
        result["terminal"] = str(run.get("status") or "") in MLFLOW_TERMINAL_RUN_STATUSES
        result["run"] = run
        return result

    def _redacted(self, exc: object) -> str:
        """Exception text with the configured server URIs/hosts stripped —
        raw httpx errors embed full request URLs, and these strings are
        persisted into agent- and UI-visible state."""
        message = str(exc)
        for uri in (self._control_uri, self.server_uri, self.tracking_uri):
            if not uri:
                continue
            message = message.replace(uri, "<mlflow-server>")
            host = urlsplit(uri).netloc
            if host:
                message = message.replace(host, "<mlflow-server>")
        return message

    def _read_run(
        self, *, client: httpx.Client, base: str, run_id: str
    ) -> dict[str, object]:
        response = client.get(
            f"{base}/api/2.0/mlflow/runs/get",
            params={"run_id": run_id},
        )
        response.raise_for_status()
        run = response.json().get("run") or {}
        info = run.get("info") or {}
        mlflow_experiment_id = str(info.get("experiment_id") or "")
        record: dict[str, object] = {
            "run_id": str(info.get("run_id") or info.get("run_uuid") or run_id),
            "run_name": str(info.get("run_name") or ""),
            "status": str(info.get("status") or ""),
            "artifact_uri": str(info.get("artifact_uri") or ""),
            "created_at": _mlflow_ms_to_iso(info.get("start_time")),
            "ended_at": _mlflow_ms_to_iso(info.get("end_time")),
        }
        if mlflow_experiment_id:
            record["experiment_id"] = mlflow_experiment_id
        if self.dashboard_url and mlflow_experiment_id and record["run_id"]:
            record["dashboard_run_url"] = (
                f"{self.dashboard_url}/#/experiments/"
                f"{mlflow_experiment_id}/runs/{record['run_id']}"
            )
        return record

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

    def results_metrics(
        self,
        *,
        project_id: str,
        experiment_id: str,
        include_history: bool = True,
    ) -> dict[str, object]:
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
        try:
            snapshot = snapshot_mlflow(
                read_uri,
                experiment_name=context.experiment_name,
                **({"include_history": False} if not include_history else {}),
            )
        except MlflowSnapshotError:
            return {
                "experiment_id": experiment_id,
                "available": False,
                "source": "mlflow",
                "hint": "MLflow unreachable.",
            }
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

    def namespace_experiments(self, *, project_id: str) -> list[dict[str, object]]:
        """List experiment metadata under one project's MLflow namespace."""
        read_uri = self.server_uri or self.tracking_uri
        if not read_uri:
            return []
        try:
            experiments = search_mlflow_experiments(
                read_uri, name_like=f"rp/{project_id}/%"
            )
        except MlflowSnapshotError:
            return []
        result: list[dict[str, object]] = []
        for experiment in experiments:
            experiment_id = str(experiment.get("experiment_id") or "")
            name = str(experiment.get("name") or "")
            if not experiment_id or not name:
                continue
            entry: dict[str, object] = {
                "name": name,
                "experiment_id": experiment_id,
            }
            if self.dashboard_url:
                entry["dashboard_experiment_url"] = (
                    f"{self.dashboard_url}/#/experiments/{experiment_id}"
                )
            result.append(entry)
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
