"""The HTTP task channel + provision handshake over the wire (plan Phase 8).

The in-process channel dispatches synchronously; the HTTP sibling has the cloud
enqueue, the daemon long-poll, execute, and ack — the cloud never dials in. This
proves the round-trip over the HttpTaskQueue, and that task payloads stay
JSON-serializable.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.dataplane.http_channel import (
    DaemonTaskLoop,
    HttpTaskChannel,
    HttpTaskQueue,
)
from backend.utils import PermissionDeniedError, ValidationError


class HttpTaskQueueRoundTripTest(unittest.TestCase):
    def test_enqueue_poll_execute_ack_round_trips_the_result(self) -> None:
        queue = HttpTaskQueue()
        channel = HttpTaskChannel(queue=queue)

        executed: list[tuple[str, dict, str | None]] = []

        def executor(task_type, payload, deadline):
            executed.append((task_type, payload, deadline))
            return {"pulled": 3, "ok": True}

        def poll(wait):
            return queue.poll(wait_seconds=wait)

        def ack(*, task_id, ok, result=None, error=None):
            queue.ack(task_id=task_id, ok=ok, result=result, error=error)

        loop = DaemonTaskLoop(poll=poll, ack=ack, executor=executor)
        # Run the daemon side in a thread; the control side submits and blocks.
        runner = threading.Thread(target=lambda: loop.run_once(wait_seconds=5.0), daemon=True)
        runner.start()
        result = channel.submit(
            task_type="conn_refresh",
            payload={"row": {"experiment_id": "exp_1"}, "name": "exp-1"},
            deadline="2099-01-01T00:00:00Z",
        )
        runner.join(timeout=5.0)
        self.assertEqual(result, {"pulled": 3, "ok": True})
        self.assertEqual(executed[0][0], "conn_refresh")
        self.assertEqual(executed[0][2], "2099-01-01T00:00:00Z")  # deadline is opaque, carried

    def test_failed_task_reraises_to_the_submitter(self) -> None:
        queue = HttpTaskQueue()
        channel = HttpTaskChannel(queue=queue)

        def executor(task_type, payload, deadline):
            raise RuntimeError("refresh exploded")

        loop = DaemonTaskLoop(
            poll=lambda wait: queue.poll(wait_seconds=wait),
            ack=lambda **kw: queue.ack(**kw),
            executor=executor,
        )
        runner = threading.Thread(target=lambda: loop.run_once(wait_seconds=5.0), daemon=True)
        runner.start()
        with self.assertRaises(RuntimeError):
            channel.submit(task_type="conn_refresh", payload={"row": {"experiment_id": "x"}})
        runner.join(timeout=5.0)

    def test_timeout_when_no_daemon_picks_up(self) -> None:
        queue = HttpTaskQueue()
        channel = HttpTaskChannel(queue=queue, result_timeout_seconds=0.3)
        # No daemon loop running: the submit times out rather than blocking forever.
        with self.assertRaises(TimeoutError):
            channel.submit(task_type="conn_refresh", payload={"row": {"experiment_id": "x"}})

    def test_unknown_task_type_is_rejected_at_enqueue(self) -> None:
        queue = HttpTaskQueue()
        with self.assertRaises(ValidationError):
            queue.enqueue(task_type="reboot_vm", payload={})

    def test_unknown_non_json_payload_field_is_rejected(self) -> None:
        queue = HttpTaskQueue()

        with self.assertRaisesRegex(
            ValidationError, "not JSON-serializable: conn_refresh.callback"
        ):
            queue.enqueue(
                task_type="conn_refresh",
                payload={"row": {"experiment_id": "x"}, "callback": lambda: None},
            )

    def test_non_standard_json_number_is_rejected(self) -> None:
        queue = HttpTaskQueue()

        with self.assertRaisesRegex(ValidationError, "conn_refresh.value"):
            queue.enqueue(
                task_type="conn_refresh",
                payload={"row": {"experiment_id": "x"}, "value": float("nan")},
            )

    def test_poll_returns_none_on_timeout_with_no_tasks(self) -> None:
        queue = HttpTaskQueue()
        self.assertIsNone(queue.poll(wait_seconds=0.05))

    def test_tenant_poll_does_not_claim_untenantized_tasks(self) -> None:
        queue = HttpTaskQueue()
        queue.enqueue(task_type="conn_refresh", payload={"row": {"experiment_id": "x"}})
        self.assertIsNone(queue.poll(wait_seconds=0.05, tenant_id="tenant_a"))
        task = queue.poll(wait_seconds=0.05)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], "conn_refresh")

    def test_tenant_ack_rejects_untenantized_task(self) -> None:
        queue = HttpTaskQueue()
        queued = queue.enqueue(
            task_type="conn_refresh",
            payload={"row": {"experiment_id": "x"}},
        )
        task = queue.poll(wait_seconds=0.05)
        self.assertEqual(task["id"], queued.id)
        with self.assertRaises(PermissionDeniedError):
            queue.ack(task_id=queued.id, ok=True, tenant_id="tenant_a")
        queue.ack(task_id=queued.id, ok=True)


if __name__ == "__main__":
    unittest.main()
