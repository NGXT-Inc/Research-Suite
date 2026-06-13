"""The HTTP sibling of the in-process task channel (cloud plan Phase 8).

Fixed decision 2: every "cloud signals daemon" flow is a daemon-initiated
long-poll task channel — the cloud NEVER dials in. This module has both sides:

- ``HttpTaskQueue`` lives in the control plane. It enqueues tasks (the same
  five types as the in-process channel) and serves them to a daemon that
  long-polls; the daemon POSTs acks/results back. Payloads are JSON-serializable
  (the parachute bytes become a presigned GET the daemon downloads, per the
  Phase 4 note), unlike the in-process channel which may keep live objects.

- ``HttpTaskChannel`` is the control-plane-facing handle that mirrors
  ``InProcessTaskChannel.submit`` but ENQUEUES instead of executing inline and
  blocks (bounded) for the daemon's result. In local mode nothing constructs
  this — the synchronous in-process channel is unchanged.

- ``DaemonTaskLoop`` runs on the daemon: it long-polls the cloud, dispatches
  each task to the LocalDataPlaneWorker, and POSTs the result. It degenerates
  to the same worker-dispatch ``_execute`` shape the in-process channel uses,
  so task semantics are identical across the seam.

Deadlines are cloud-minted ISO instants the daemon treats as opaque (§3.2).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..utils import ValidationError, new_id
from .tasks import TASK_TYPES


def _json_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop non-JSON-serializable payload fields for the HTTP path.

    Phase 4/5 left live objects in some task payloads (e.g. the initial_push
    ``on_retry`` progress callback; in-process parachute_restore ``data``
    bytes). The HTTP channel serves payloads as JSON, so those fields cannot
    cross the wire — they are dropped here. The daemon executor never needs the
    callback (it runs the push directly), and parachute bytes become a
    presigned ``get_url`` the daemon downloads, set by the control plane before
    enqueue. A field that is silently load-bearing for the HTTP path would
    surface as a KeyError in the executor, not a silent wrong result.
    """
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue  # e.g. on_retry callback, raw bytes — not for the wire
        safe[key] = value
    return safe


# How long the cloud holds a long-poll open before returning empty (the daemon
# immediately re-polls). Bounded so a daemon shutdown is observed promptly.
DEFAULT_LONG_POLL_SECONDS = 25.0
# How long the control-plane submitter waits for a daemon to execute a task
# before giving up (e.g. so a final_pull can fall through to the parachute).
DEFAULT_TASK_RESULT_SECONDS = 120.0


@dataclass
class _PendingTask:
    id: str
    type: str
    payload: dict[str, Any]
    deadline: str | None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None


class HttpTaskQueue:
    """Cloud-side queue of daemon work, served over HTTP long-poll.

    Thread-safe. One queue per control process (the daemon identifies itself by
    client_id on poll; v1 has one daemon per tenant, so a single shared queue
    is correct — multi-daemon fan-out is a Phase 9 concern, seam left here).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._waiting: list[_PendingTask] = []
        self._in_flight: dict[str, _PendingTask] = {}

    def enqueue(
        self, *, task_type: str, payload: dict[str, Any], deadline: str | None = None
    ) -> _PendingTask:
        if task_type not in TASK_TYPES:
            raise ValidationError(f"unknown task type: {task_type}")
        task = _PendingTask(
            id=new_id(prefix="task"),
            type=task_type,
            payload=_json_safe_payload(payload),
            deadline=deadline,
        )
        with self._cond:
            self._waiting.append(task)
            self._cond.notify_all()
        return task

    def poll(self, *, wait_seconds: float = DEFAULT_LONG_POLL_SECONDS) -> dict[str, Any] | None:
        """Block up to ``wait_seconds`` for the next task; None if none arrives."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        with self._cond:
            while not self._waiting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            task = self._waiting.pop(0)
            self._in_flight[task.id] = task
        return {
            "id": task.id,
            "type": task.type,
            "payload": task.payload,
            "deadline": task.deadline,
        }

    def ack(self, *, task_id: str, ok: bool, result: Any = None, error: str | None = None) -> None:
        with self._cond:
            task = self._in_flight.pop(task_id, None)
        if task is None:
            return
        task.result = result
        task.error = None if ok else (error or "task failed")
        task.done.set()

    def await_result(
        self, *, task: _PendingTask, timeout_seconds: float = DEFAULT_TASK_RESULT_SECONDS
    ) -> Any:
        if not task.done.wait(timeout=timeout_seconds):
            # The daemon did not finish in budget. Surface it so the caller can
            # fall through (a final_pull → parachute branch); drop it from
            # waiting so a late daemon doesn't pick up a dead task.
            with self._cond:
                self._waiting = [t for t in self._waiting if t.id != task.id]
                self._in_flight.pop(task.id, None)
            raise TimeoutError(f"daemon task {task.type} timed out")
        if task.error is not None:
            raise RuntimeError(task.error)
        return task.result


class HttpTaskChannel:
    """Control-plane handle that submits a task to the daemon over HTTP.

    Mirrors ``InProcessTaskChannel.submit`` so SandboxService is channel-blind:
    it enqueues on the HttpTaskQueue and blocks (bounded) for the daemon's
    result. A timeout/failure raises, matching the in-process channel's
    re-raise so the reaper's final_pull→parachute branch fires identically.
    """

    def __init__(
        self, *, queue: HttpTaskQueue, result_timeout_seconds: float = DEFAULT_TASK_RESULT_SECONDS
    ) -> None:
        self.queue = queue
        self.result_timeout_seconds = result_timeout_seconds

    def submit(
        self, *, task_type: str, payload: dict[str, Any], deadline: str | None = None
    ) -> Any:
        task = self.queue.enqueue(task_type=task_type, payload=payload, deadline=deadline)
        return self.queue.await_result(
            task=task, timeout_seconds=self.result_timeout_seconds
        )


# A function the daemon loop calls per task: (task_type, payload, deadline) ->
# result. The daemon binds this to its LocalDataPlaneWorker dispatch.
TaskExecutor = Callable[[str, dict[str, Any], str | None], Any]


class DaemonTaskLoop:
    """Daemon-side long-poll loop: poll the cloud, execute, ack (plan Phase 8).

    Runs in a background thread. The cloud never connects inbound — this loop
    is the only direction of travel. Each task is executed by the injected
    ``executor`` (the worker dispatch) and the result POSTed back as an ack.
    """

    def __init__(
        self,
        *,
        poll: Callable[[float], dict[str, Any] | None],
        ack: Callable[..., None],
        executor: TaskExecutor,
        poll_seconds: float = DEFAULT_LONG_POLL_SECONDS,
    ) -> None:
        self._poll = poll
        self._ack = ack
        self._executor = executor
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="daemon-task-loop", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def run_once(self, *, wait_seconds: float | None = None) -> bool:
        """Poll once and execute one task if present. Returns True if it ran.

        Exposed for deterministic tests (drive the loop without the thread).
        """
        task = self._poll(self._poll_seconds if wait_seconds is None else wait_seconds)
        if task is None:
            return False
        self._dispatch(task=task)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._poll(self._poll_seconds)
            except Exception:  # noqa: BLE001 — a transient poll failure must not kill the loop
                time.sleep(1.0)
                continue
            if task is None:
                continue
            self._dispatch(task=task)

    def _dispatch(self, *, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        try:
            result = self._executor(
                str(task.get("type") or ""),
                dict(task.get("payload") or {}),
                task.get("deadline"),
            )
        except Exception as exc:  # noqa: BLE001 — report failure as an ack
            self._ack(task_id=task_id, ok=False, error=str(exc))
            return
        self._ack(task_id=task_id, ok=True, result=result)
