"""The control→data task channel.

Every "control plane signals the data plane" flow is a task: the control plane
enqueues, the data plane executes and acks. The channel only handles local
conn/dashboard maintenance; sandbox file movement is explicit SSH work by the
agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..utils import ValidationError, new_id

if TYPE_CHECKING:
    # Typing-only: a runtime import would load the local worker stack
    # (workspace, dashboard tunnels) and break import-time separation for
    # `backend.dataplane` as an entry point.
    from .worker import DataPlaneWorker


TASK_TYPES: frozenset[str] = frozenset(
    {
        "conn_refresh",
        "teardown",
    }
)


@dataclass(frozen=True)
class Task:
    """One unit of data-plane work, minted by the control plane."""

    id: str
    type: str
    payload: dict[str, Any]
    # Cloud-authoritative ISO instant; opaque to the data plane.
    deadline: str | None = None


class InProcessTaskChannel:
    """Local-mode channel: enqueue == execute == ack, in submission order.

    Dispatches to the worker synchronously the moment a task is submitted, so
    callers observe exactly the ordering they had before the channel existed.
    Every task and its ack are recorded in memory — the observation seam the
    tests (and, later, the split-mode ack protocol) rely on. A failing task
    re-raises to the submitter after recording the failed ack, preserving the
    callers' existing error handling.
    """

    def __init__(self, *, worker: DataPlaneWorker) -> None:
        self.worker = worker
        # (task, ack) pairs in dispatch order.
        self.history: list[tuple[Task, dict[str, Any]]] = []

    def submit(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        deadline: str | None = None,
        tenant_id: str | None = None,  # noqa: ARG002 - HTTP channel uses this
    ) -> Any:
        if task_type not in TASK_TYPES:
            raise ValidationError(f"unknown task type: {task_type}")
        task = Task(
            id=new_id(prefix="task"),
            type=task_type,
            payload=dict(payload),
            deadline=deadline,
        )
        try:
            result = self._execute(task=task)
        except BaseException as exc:
            self.history.append(
                (task, {"task_id": task.id, "ok": False, "error": str(exc)})
            )
            raise
        self.history.append((task, {"task_id": task.id, "ok": True}))
        return result

    def _execute(self, *, task: Task) -> Any:
        payload = task.payload
        if task.type == "conn_refresh":
            # Re-render the agent's conn file (and ssh command) for a row
            # whose tunnel endpoint moved.
            return self.worker.sandbox_enrichment(
                row=payload["row"],
                name=str(payload.get("name") or ""),
                use_sandbox_uid_command=bool(payload.get("use_sandbox_uid_command")),
            )
        if task.type == "teardown":
            # sandbox_id is None when the row itself was missing: skip tunnel
            # teardown but still drop the conn file (pre-channel behavior).
            sandbox_id = payload.get("sandbox_id")
            if sandbox_id is not None:
                self.worker.stop_dashboards(sandbox_id=str(sandbox_id))
                self.worker.stop_mlflow_access(sandbox_id=str(sandbox_id))
            self.worker.remove_conn_file(
                experiment_id=str(payload["experiment_id"]),
                sandbox_uid=str(payload.get("sandbox_uid") or ""),
                remove_experiment_alias=bool(
                    payload.get("remove_experiment_alias", True)
                ),
            )
            return None
        raise ValidationError(f"unknown task type: {task.type}")
