from __future__ import annotations

import unittest

from merv.brain.sandbox.lifecycle_reducer import (
    reap_decision,
    reconcile_decision,
    release_decision,
)


class SandboxLifecycleReducerTest(unittest.TestCase):
    def test_unknown_liveness_never_terminates(self) -> None:
        decision = reconcile_decision(
            row={"status": "running", "sandbox_id": "sb", "sandbox_uid": "uid"},
            alive=None,
        )
        self.assertEqual(decision.intents, ())
        self.assertIsNone(decision.event)

    def test_confirmed_dead_row_marks_and_emits(self) -> None:
        decision = reconcile_decision(
            row={"status": "running", "sandbox_id": "sb", "sandbox_uid": "uid"},
            alive=False,
        )
        self.assertEqual([item.kind for item in decision.intents], ["mark_terminated"])
        self.assertEqual(decision.event.type, "sandbox.expired")

    def test_missing_provision_job_cleans_before_failure(self) -> None:
        decision = reconcile_decision(
            row={"status": "provisioning", "sandbox_uid": "uid"},
            alive=None,
            job_live=False,
        )
        self.assertEqual(
            [item.kind for item in decision.intents],
            ["cleanup_orphan", "mark_failed"],
        )
        self.assertEqual(decision.event.type, "sandbox.failed")

    def test_failed_termination_keeps_reap_retryable(self) -> None:
        decision = reap_decision(
            row={"sandbox_id": "sb", "sandbox_uid": "uid"},
            outcome="maybe_alive",
            event_type="sandbox.expired",
        )
        self.assertEqual(decision.intents, ())
        self.assertFalse(decision.event.payload["reaped"])

    def test_release_marks_only_after_provider_confirmation(self) -> None:
        uncertain = release_decision(
            row={"sandbox_id": "sb", "sandbox_uid": "uid"},
            outcome="maybe_alive",
            active_experiment_ids=["exp_1"],
        )
        confirmed = release_decision(
            row={"sandbox_id": "sb", "sandbox_uid": "uid"},
            outcome="stopped",
            active_experiment_ids=["exp_1"],
        )
        self.assertEqual(uncertain.intents, ())
        self.assertEqual(uncertain.event.type, "sandbox.release_failed")
        self.assertEqual(confirmed.intents[0].kind, "mark_terminated")
        self.assertEqual(confirmed.event.type, "sandbox.released")


if __name__ == "__main__":
    unittest.main()
