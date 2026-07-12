from __future__ import annotations

import unittest

from backend.control.control_runtime import ControlTaskChannel
from backend.utils import ValidationError


class ControlTaskChannelTest(unittest.TestCase):
    def test_conn_refresh_returns_empty_enrichment_without_daemon_wait(self) -> None:
        channel = ControlTaskChannel()

        result = channel.submit(
            task_type="conn_refresh",
            payload={"row": {"sandbox_uid": "sbx_1"}},
        )

        self.assertEqual(result, {})
        self.assertEqual(channel.history[0][0], "conn_refresh")

    def test_teardown_is_noop_without_daemon_wait(self) -> None:
        channel = ControlTaskChannel()

        result = channel.submit(
            task_type="teardown",
            payload={"experiment_id": "exp_1", "sandbox_uid": "sbx_1"},
        )

        self.assertIsNone(result)
        self.assertEqual(channel.history[0][0], "teardown")

    def test_submit_records_payload_snapshot(self) -> None:
        channel = ControlTaskChannel()
        payload = {"row": {"sandbox_uid": "sbx_1"}}

        channel.submit(task_type="conn_refresh", payload=payload)
        payload["row"]["sandbox_uid"] = "mutated"

        self.assertEqual(channel.history[0][1], {"row": {"sandbox_uid": "sbx_1"}})

    def test_deadline_and_tenant_do_not_wait_for_daemon(self) -> None:
        channel = ControlTaskChannel()

        result = channel.submit(
            task_type="teardown",
            payload={"sandbox_uid": "sbx_1"},
            deadline="2000-01-01T00:00:00Z",
            tenant_id="tenant_1",
        )

        self.assertIsNone(result)
        self.assertEqual(channel.history, [("teardown", {"sandbox_uid": "sbx_1"})])

    def test_unknown_task_type_is_rejected(self) -> None:
        channel = ControlTaskChannel()

        with self.assertRaises(ValidationError):
            channel.submit(task_type="unexpected", payload={})


if __name__ == "__main__":
    unittest.main()
