"""Sandbox lifecycle is not an experiment-status transition.

Pins the correctness boundary: sandbox code must not write experiments or call
a hidden experiment transition. Experiments move through explicit workflow
transitions; sandboxes are linked through sandbox_attachments only.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.research_core.domain.workflow_gates import (
    GATE_TABLE,
    SYSTEM_TRANSITIONS,
    TRANSITION_GRAPH,
    TRANSITION_REQUIREMENTS,
)
from merv.brain.kernel.utils import WorkflowError
from tests.paths import BACKEND_ROOT


class SystemTransitionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.call("project", action="create", name="System Transitions")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self, *, status: str = "ready_to_run") -> str:
        exp_id = self.call("experiment.create", name="exp-1", project_id=self.project_id, intent="x")["id"]
        if status != "planned":
            with self.app.store.transaction() as conn:
                conn.execute("UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id))
        return exp_id

    def _transition_events(self, exp_id: str) -> list[dict]:
        conn = self.app.store.connect()
        try:
            rows = conn.execute(
                """
                SELECT payload_json FROM events
                WHERE type = 'experiment.transitioned' AND target_id = ?
                ORDER BY id
                """,
                (exp_id,),
            ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]
        finally:
            conn.close()


class SandboxDrivenTransitionTest(SystemTransitionTestBase):
    def test_sandbox_request_does_not_transition_experiment(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")
        self.assertEqual(self._transition_events(exp_id), [])

    def test_sandbox_reuse_still_does_not_transition_experiment(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(self._transition_events(exp_id), [])

    def test_reaper_expiry_does_not_transition_experiment(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", created["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")
        self.assertEqual(self._transition_events(exp_id), [])

    def test_no_sandbox_system_transitions_are_registered(self) -> None:
        self.assertEqual(SYSTEM_TRANSITIONS, frozenset())

    def test_no_system_transitions_in_discovery(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        names = {t["transition"] for t in state["allowed_transitions"]}
        self.assertIn("start_running", names)
        self.assertFalse(names & SYSTEM_TRANSITIONS)

    def test_experiment_service_has_no_system_transition_escape_hatch(self) -> None:
        self.assertFalse(hasattr(self.app.experiments, "apply_system_transition"))

    def test_sandbox_code_has_no_raw_experiment_writes(self) -> None:
        # The boundary itself: sandbox services must not UPDATE/INSERT the
        # experiments table.
        source = (BACKEND_ROOT / "sandbox" / "sandboxes.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("UPDATE experiments", source)
        self.assertNotIn("INSERT INTO experiments", source)


class GateTableConsistencyTest(SystemTransitionTestBase):
    def test_transition_graph_and_prose_derive_from_gate_table(self) -> None:
        for status, forward in GATE_TABLE.items():
            self.assertEqual(TRANSITION_GRAPH[(status, forward.name)], forward.to_status)
            if forward.requires_prose:
                self.assertEqual(TRANSITION_REQUIREMENTS[forward.name], forward.requires_prose)
        for (frm, name), nxt in TRANSITION_GRAPH.items():
            if name in SYSTEM_TRANSITIONS:
                continue
            self.assertEqual(GATE_TABLE[frm].name, name)
            self.assertEqual(GATE_TABLE[frm].to_status, nxt)

    def test_enforcement_fact_drives_application_guidance(self) -> None:
        # Research owns the unmet role/error; Application owns the tool advice.
        exp_id = self._experiment(status="planned")
        plan_req = GATE_TABLE["planned"].requirements[0]
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=exp_id,
                transition="submit_design",
            )
        self.assertEqual(str(ctx.exception), plan_req.error)
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf["workflow"]
        self.assertEqual(workflow["current_gate"], plan_req.gate)
        self.assertEqual(
            workflow["next_action"], "write_and_associate_plan_resource"
        )
        self.assertEqual(workflow["allowed_actions"], ["resource.register"])
        self.assertEqual(workflow["missing_evidence"], [plan_req.missing])


if __name__ == "__main__":
    unittest.main()
