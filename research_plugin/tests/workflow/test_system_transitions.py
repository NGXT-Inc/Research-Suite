"""System (sandbox-lifecycle) transitions route through the workflow engine.

Covers the correctness boundary: the sandbox registry must never write the
experiments table directly. Status changes driven by sandbox lifecycle go
through ExperimentService.apply_system_transition, which keeps the
experiment.transitioned event log complete and keeps TRANSITION_GRAPH the
single writer of experiment status. Also pins the declarative gate table as
the shared source for enforcement, guidance, and discovery.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.domain.workflow_gates import (
    GATE_TABLE,
    SYSTEM_TRANSITIONS,
    TRANSITION_GRAPH,
    TRANSITION_REQUIREMENTS,
)
from backend.utils import ValidationError, WorkflowError
from tests.paths import SERVICES_ROOT


class SystemTransitionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.call("project.create", name="System Transitions")["id"]

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
    def test_sandbox_request_transitions_experiment_via_event_log(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")
        events = self._transition_events(exp_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from"], "ready_to_run")
        self.assertEqual(events[0]["to"], "running")
        self.assertEqual(events[0]["transition"], "sandbox_started")
        self.assertTrue(events[0]["system"])

    def test_sandbox_reuse_does_not_duplicate_the_transition_event(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        # Second request reuses the live sandbox; the experiment is already
        # running, so the system transition is a no-op — no second event.
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        events = [e for e in self._transition_events(exp_id) if e["transition"] == "sandbox_started"]
        self.assertEqual(len(events), 1)

    def test_reaper_expiry_reverts_and_logs_system_transition(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                ("2000-01-01T00:00:00Z", exp_id),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")
        expired = [e for e in self._transition_events(exp_id) if e["transition"] == "sandbox_expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["from"], "running")
        self.assertEqual(expired[0]["to"], "ready_to_run")
        self.assertTrue(expired[0]["system"])
        self.assertIn("reason", expired[0])

    def test_agent_cannot_call_system_transitions(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        for transition in sorted(SYSTEM_TRANSITIONS):
            # Layer 1: the tool contract's Literal whitelist rejects the name
            # before it ever reaches the service.
            with self.assertRaises(ValidationError):
                self.call(
                    "experiment.transition",
                    project_id=self.project_id,
                    experiment_id=exp_id,
                    transition=transition,
                )
            # Layer 2: a direct service call (bypassing contracts) is rejected
            # by the workflow engine itself.
            with self.assertRaises(WorkflowError) as ctx:
                self.app.experiments.transition(
                    project_id=self.project_id,
                    experiment_id=exp_id,
                    transition=transition,
                )
            self.assertIn("system-driven", str(ctx.exception))
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_system_transitions_hidden_from_discovery(self) -> None:
        exp_id = self._experiment(status="ready_to_run")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        names = {t["transition"] for t in state["allowed_transitions"]}
        self.assertIn("start_running", names)
        self.assertFalse(names & SYSTEM_TRANSITIONS)

    def test_apply_system_transition_is_a_tolerated_noop_when_inapplicable(self) -> None:
        exp_id = self._experiment(status="planned")
        applied = self.app.experiments.apply_system_transition(
            experiment_id=exp_id, transition="sandbox_started"
        )
        self.assertFalse(applied)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "planned")
        self.assertEqual(self._transition_events(exp_id), [])
        # Unknown experiments no-op too (a late sandbox event must never raise).
        self.assertFalse(
            self.app.experiments.apply_system_transition(
                experiment_id="exp_nope", transition="sandbox_started"
            )
        )

    def test_apply_system_transition_rejects_agent_transition_names(self) -> None:
        exp_id = self._experiment(status="planned")
        with self.assertRaises(WorkflowError):
            self.app.experiments.apply_system_transition(
                experiment_id=exp_id, transition="submit_design"
            )

    def test_sandbox_code_has_no_raw_experiment_writes(self) -> None:
        # The boundary itself: the sandbox registry must not UPDATE/INSERT the
        # experiments table. Status changes go through apply_system_transition.
        source = (SERVICES_ROOT / "sandbox" / "sandboxes.py").read_text(
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

    def test_enforcement_error_and_guidance_share_the_table_entry(self) -> None:
        # The same RoleRequirement drives both the WorkflowError text and the
        # status_and_next gate payload, so they cannot drift.
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
        self.assertEqual(workflow["next_action"], plan_req.action)
        self.assertEqual(workflow["allowed_actions"], list(plan_req.allowed))
        self.assertEqual(workflow["missing_evidence"], [plan_req.missing])


if __name__ == "__main__":
    unittest.main()
