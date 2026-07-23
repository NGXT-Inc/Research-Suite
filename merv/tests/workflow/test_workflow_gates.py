from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain, upload_token
from merv.brain.research_core.domain.artifacts import plan_sections_missing, report_figure_links
from merv.brain.research_core.domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    infer_claim_status_from_conclusion,
)
from merv.brain.kernel.utils import PermissionDeniedError, ValidationError, WorkflowError

# A plan that satisfies the required spine (Summary; Objective & hypothesis;
# Evaluation), so submit_design's section lint passes.
VALID_PLAN = (
    "## Summary\n"
    "A toy experiment used by the gate tests.\n\n"
    "## Objective & hypothesis\n"
    "Test that the threshold rule beats the majority baseline.\n\n"
    "## Evaluation\n"
    "Metric: accuracy vs the majority-class baseline; success if accuracy > 0.6.\n"
)

# A results report that satisfies the report lint (required spine + a metrics
# table), so submit_results passes in tests that drive the full loop.
VALID_REPORT = (
    "## Summary\n"
    "Ran the toy experiment per the approved plan.\n\n"
    "## Results\n\n"
    "| Metric | Target | Achieved |\n"
    "|--------|--------|----------|\n"
    "| accuracy | 0.60 | 0.72 |\n\n"
    "## Deviations from plan\n"
    "None.\n\n"
    "## Conclusion\n"
    "Decision rule met: accuracy 0.72 > 0.6 threshold.\n"
)

# A logic graph that satisfies the envelope lint (valid JSON, ≤16 nodes, DAG),
# so submit_results passes in tests that drive the full loop.
VALID_GRAPH = (
    '{"version": 1, "nodes": ['
    '{"id": "obj", "kind": "objective", "label": "Beat the majority baseline"},'
    '{"id": "out", "kind": "outcome", "label": "Threshold met at 0.72"}],'
    ' "edges": [{"from": "obj", "to": "out", "label": "confirmed by"}]}\n'
)


class WorkflowGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Gate Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _submit(self, *, exp_id: str, path: str, role: str, body: str) -> dict:
        return self.app.submit_artifact(
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
            path=path,
            body=body,
        )

    def _open_review_session(self, *, exp_id: str, role: str) -> str:
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=f"{role}-reviewer",
        )
        return session["review_session_id"]

    def _pass_review(self, *, exp_id: str, role: str) -> None:
        session_id = self._open_review_session(exp_id=exp_id, role=role)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The plan and results check out, so the attempt stands as reported.",
        )

    def _drive_to_running(
        self, *, name: str = "exp-1", intent: str = "Rejection routing."
    ) -> str:
        exp_id = self.call(
            "experiment.create",
            name=name,
            project_id=self.project_id,
            intent=intent,
        )["id"]
        self._submit(exp_id=exp_id, path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_ready_to_run")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="start_running")
        return exp_id

    def _drive_to_running_with_result(self) -> str:
        exp_id = self._drive_to_running()
        self._submit(exp_id=exp_id, path="results.json", role="result", body="{\"metric\": 1}\n")
        return exp_id

    def _drive_to_experiment_review(self) -> str:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        return exp_id

    def _drive_to_complete(
        self, *, conclusion: str = "", tested_claim_ids: list[str] | None = None
    ) -> str:
        exp_id = self.call(
            "experiment.create",
            name="exp-2",
            project_id=self.project_id,
            intent="Full loop.",
            tested_claim_ids=tested_claim_ids or [],
        )["id"]
        self._submit(exp_id=exp_id, path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_ready_to_run")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="start_running")
        self._submit(exp_id=exp_id, path="results.json", role="result", body="{\"metric\": 1}\n")
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")
        evidence = {"conclusion": conclusion} if conclusion else None
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="complete",
            evidence=evidence,
        )
        return exp_id

    # ---- plan gate ----

    def test_submit_design_requires_a_plan_resource(self) -> None:
        exp = self.call("experiment.create", name="exp-3", project_id=self.project_id, intent="No plan yet.")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        # Reaching design_review must be impossible without a plan, so ready_to_run is too.
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])["status"],
            "planned",
        )
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.assertEqual(out["status"], "design_review")

    def test_pinned_plan_is_gate_evidence_without_any_live_file(self) -> None:
        # Submission pins bytes server-side; no working-tree file ever existed,
        # and the gate never probes disk.
        exp = self.call(
            "experiment.create",
            name="missing-live-plan",
            project_id=self.project_id,
            intent="Pinned evidence survives its working file.",
        )
        self._submit(
            exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN
        )
        state = self.call(
            "experiment.get_state",
            project_id=self.project_id,
            experiment_id=exp["id"],
        )
        item = state["gate_checklist"]["items"][0]
        self.assertEqual(item["status"], "valid")
        self.assertTrue(item["satisfied"])
        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp["id"],
            transition="submit_design",
        )
        self.assertEqual(out["status"], "design_review")

    def test_resubmit_supersedes_and_newest_submission_is_linted(self) -> None:
        exp = self.call(
            "experiment.create",
            name="association-order",
            project_id=self.project_id,
            intent="Newest submission wins the gate.",
        )
        self._submit(exp_id=exp["id"], path="plan-a.md", role="plan", body=VALID_PLAN)
        self._submit(
            exp_id=exp["id"],
            path="plan-b.md",
            role="plan",
            body="## Summary\nIncomplete newer plan.\n",
        )
        state = self.call(
            "experiment.get_state",
            project_id=self.project_id,
            experiment_id=exp["id"],
        )
        plan = state["gate_checklist"]["items"][0]
        self.assertEqual(plan["status"], "invalid")
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=exp["id"],
                transition="submit_design",
            )
        self.assertEqual(str(ctx.exception), plan["problems"][0])
        # Resubmitting plan-a supersedes its slot and becomes the newest
        # plan-role submission, so the gate lints the valid document again.
        self._submit(exp_id=exp["id"], path="plan-a.md", role="plan", body=VALID_PLAN)
        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp["id"],
            transition="submit_design",
        )
        self.assertEqual(out["status"], "design_review")

    def test_workflow_surfaces_plan_gate_with_folder_guidance(self) -> None:
        exp = self.call("experiment.create", name="plan-gate", project_id=self.project_id, intent="No plan yet.")
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp["id"])
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "plan_required")
        self.assertEqual(workflow["next_action"], "write_and_submit_plan")
        guidance = workflow["artifact_guidance"]
        self.assertEqual(guidance["role"], "plan")
        # The guidance names the experiment's actual folder, not a placeholder.
        self.assertIn("experiments/plan-gate/plan.md", guidance["guidance"])

    def test_submit_design_requires_plan_spine_sections(self) -> None:
        exp = self.call("experiment.create", name="exp-4", project_id=self.project_id, intent="Thin plan.")
        # A plan resource exists, but the file lacks the required spine sections.
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body="just some loose notes\n")
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.assertIn("missing required sections", str(ctx.exception))
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])["status"],
            "planned",
        )
        # Filling in the spine unblocks it. The lint reads the SUBMITTED bytes,
        # so the fix must be re-associated to count (fix-and-resubmit).
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.assertEqual(out["status"], "design_review")

    def test_submit_design_blocks_plan_figures_missing_from_submission(self) -> None:
        # The upload mints figure tokens for every markdown image link; until
        # the figure bytes are pushed, the gate must still block.
        exp = self.call("experiment.create", name="plan-fig", project_id=self.project_id, intent="Plan figure gate.")
        plan = VALID_PLAN + "\n![arch](figures/diagram.png)\n"
        submitted = self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=plan)
        self.assertEqual(
            [fig["link_path"] for fig in submitted["figures"]],
            ["figures/diagram.png"],
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.assertIn("figures/diagram.png", str(ctx.exception))

    def test_plan_sections_missing_detects_empty_and_absent(self) -> None:
        self.assertEqual(plan_sections_missing(VALID_PLAN), [])
        # Heading present but body is only template guidance (HTML comment) ⇒ empty.
        only_comments = (
            "## Summary\n<!-- fill me in -->\n\n"
            "## Objective & hypothesis\nReal objective.\n\n"
            "## Evaluation\nReal evaluation.\n"
        )
        self.assertEqual(plan_sections_missing(only_comments), ["Summary"])
        # Absent headings are reported by canonical name; '&'/'and' both match.
        self.assertEqual(
            plan_sections_missing("## Summary\nx\n\n## Objective and hypothesis\ny\n"),
            ["Evaluation"],
        )
        self.assertEqual(
            set(plan_sections_missing("# Title only\n")),
            {"Summary", "Objective & hypothesis", "Evaluation"},
        )

    def test_report_figure_links_preserves_keyword_compatibility(self) -> None:
        self.assertEqual(
            report_figure_links(report_text="![result](figures/result.png)"),
            ["figures/result.png"],
        )

    # ---- transition discovery (allowed_transitions + helpful errors) ----

    def test_get_state_surfaces_allowed_transitions_with_requirements(self) -> None:
        exp = self.call("experiment.create", name="exp-5", project_id=self.project_id, intent="discover")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        trans = {t["transition"]: t for t in state["allowed_transitions"]}
        self.assertIn("submit_design", trans)
        self.assertEqual(trans["submit_design"]["leads_to"], "design_review")
        self.assertIn("requires", trans["submit_design"])  # precondition surfaced up front
        self.assertIn("abandon", trans)  # always available from a non-terminal state
        checklist = state["gate_checklist"]
        self.assertEqual(checklist["transition"], "submit_design")
        self.assertFalse(checklist["ready"])
        self.assertEqual(checklist["items"][0]["id"], "artifact:plan")
        self.assertEqual(checklist["items"][0]["status"], "missing")
        self.assertIn("experiment plan artifact", checklist["items"][0]["missing"])

    def test_disallowed_transition_error_lists_allowed_options(self) -> None:
        exp = self.call("experiment.create", name="exp-6", project_id=self.project_id, intent="bad jump")
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition", project_id=self.project_id,
                experiment_id=exp["id"], transition="start_running",
            )
        msg = str(ctx.exception)
        self.assertIn("not allowed", msg)
        self.assertIn("submit_design", msg)  # tells the agent what IS allowed from here

    def test_retry_running_keeps_current_attempt_and_records_infra_context(self) -> None:
        exp_id = self._drive_to_running(
            name="infra-retry",
            intent="Retry current approved attempt after VM failure.",
        )
        before = self.call(
            "experiment.get_state",
            project_id=self.project_id,
            experiment_id=exp_id,
        )
        transitions = {t["transition"]: t for t in before["allowed_transitions"]}
        self.assertEqual(transitions["retry_running"]["leads_to"], "running")
        self.assertIn("attempt_index is unchanged", transitions["retry_running"]["requires"])

        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="retry_running",
            evidence={
                "reason": "sandbox expired",
                "detail": "provider terminated the VM before outputs were retained",
            },
        )

        self.assertEqual(out["status"], "running")
        self.assertEqual(out["attempt_index"], before["attempt_index"])
        self.assertIn("Infrastructure retry requested", out["revision_context"])
        self.assertIn("sandbox expired", out["revision_context"])
        self.assertIn("provider terminated", out["revision_context"])

        wf = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=exp_id,
        )["workflow"]
        self.assertEqual(wf["current_gate"], "execution_ready")
        self.assertEqual(wf["next_action"], "run_experiment_and_retain_results")
        self.assertIn("experiment.transition", wf["allowed_actions"])
        self.assertIn("Infrastructure retry requested", wf["revision_context"])

    def test_retry_running_is_rejected_outside_running_status(self) -> None:
        exp = self.call(
            "experiment.create",
            name="retry-too-soon",
            project_id=self.project_id,
            intent="Cannot retry before execution.",
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=exp["id"],
                transition="retry_running",
            )
        self.assertIn("not allowed from 'planned'", str(ctx.exception))

    def test_terminal_experiment_has_no_allowed_transitions(self) -> None:
        exp = self.call("experiment.create", name="exp-7", project_id=self.project_id, intent="dead end")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="abandon")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        self.assertEqual(state["allowed_transitions"], [])
        self.assertEqual(state["gate_checklist"]["transition"], None)
        self.assertTrue(state["gate_checklist"]["ready"])

    def test_experiment_create_enforces_active_experiment_cap(self) -> None:
        for index in range(ACTIVE_EXPERIMENT_CAP):
            self.call(
                "experiment.create",
                name=f"active-{index}",
                project_id=self.project_id,
                intent="Keep this experiment active.",
            )

        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.create",
                name="active-over-cap",
                project_id=self.project_id,
                intent="Should wait for a slot.",
            )
        self.assertEqual(
            str(ctx.exception),
            (
                "active experiment cap reached: project has 7 active "
                "experiments; finish one before creating another."
            ),
        )

        experiments = self.call("experiment.list", project_id=self.project_id)["experiments"]
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=experiments[0]["id"],
            transition="abandon",
        )
        created = self.call(
            "experiment.create",
            name="active-after-slot",
            project_id=self.project_id,
            intent="A slot is available.",
        )
        self.assertEqual(created["name"], "active-after-slot")

    # ---- terminal transitions ----

    def test_terminal_experiment_rejects_abandon(self) -> None:
        exp_id = self._drive_to_complete()
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "complete",
        )
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="abandon")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_failed")
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "complete",
        )

    def test_abandoned_experiment_cannot_be_re_abandoned(self) -> None:
        exp = self.call("experiment.create", name="exp-8", project_id=self.project_id, intent="Dead end.")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="abandon")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="abandon")

    # ---- conclusion ----

    def test_complete_persists_conclusion(self) -> None:
        exp_id = self._drive_to_complete(conclusion="The claim is supported by results.json.")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["conclusion"], "The claim is supported by results.json.")

    def test_complete_suggests_scoped_claim_update_for_negative_result(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="The threshold rule improves accuracy.",
        )
        exp_id = self._drive_to_complete(
            conclusion="The result does not support the claim; accuracy failed to improve.",
            tested_claim_ids=[claim["id"]],
        )

        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        suggestion = state["claim_update_suggestions"][0]
        self.assertEqual(suggestion["tool"], "claim.update")
        self.assertEqual(suggestion["suggested_status"], "weakened")
        self.assertTrue(suggestion["requires_confirmation"])
        self.assertEqual(
            suggestion["arguments"],
            {
                "project_id": self.project_id,
                "claim_id": claim["id"],
                "status": "weakened",
            },
        )

    def test_complete_suggests_scoped_claim_update_for_supported_result(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="The threshold rule improves accuracy.",
        )
        exp_id = self._drive_to_complete(
            conclusion="Accuracy improved and the claim is supported by results.json.",
            tested_claim_ids=[claim["id"]],
        )

        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        suggestion = state["claim_update_suggestions"][0]
        self.assertEqual(suggestion["suggested_status"], "supported")
        self.assertEqual(suggestion["arguments"]["claim_id"], claim["id"])
        self.assertEqual(suggestion["arguments"]["status"], "supported")

    # ---- results report gate ----

    def test_submit_results_requires_report_resource(self) -> None:
        exp_id = self._drive_to_running_with_result()
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertIn("report", str(ctx.exception))
        self.assertIn("report-template", str(ctx.exception))
        # Adding a valid report (and the logic graph) unblocks the same transition.
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertEqual(out["status"], "experiment_review")

    def test_submit_results_lints_report_content(self) -> None:
        exp_id = self._drive_to_running_with_result()
        # Sections present but Conclusion left empty. No metrics-table lint:
        # the system metrics exhibit, not an agent-written table, is the
        # record of quantitative attempts.
        bad_report = (
            "## Summary\nRan it.\n\n"
            "## Results\nIt went well.\n\n"
            "## Deviations from plan\nNone.\n\n"
            "## Conclusion\n<!-- todo -->\n"
        )
        self._submit(exp_id=exp_id, path="report.md", role="report", body=bad_report)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        msg = str(ctx.exception)
        self.assertIn("Conclusion", msg)  # empty section reported
        self.assertNotIn("markdown table", msg)  # table shape is not policed
        # Editing the live file alone changes nothing — the lint reads the
        # submitted bytes; re-associating the fix unblocks.
        (self.repo / "report.md").write_text(VALID_REPORT)
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertEqual(out["status"], "experiment_review")

    def test_report_image_links_must_have_submitted_figures(self) -> None:
        exp_id = self._drive_to_running_with_result()
        report = VALID_REPORT + "\n## Figures\n\n![loss curve](figures/loss.png)\n"
        submitted = self._submit(exp_id=exp_id, path="report.md", role="report", body=report)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        # The figure bytes were never pushed, so the gate blocks.
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertIn("figures/loss.png", str(ctx.exception))
        # Pushing the figure through its one-time token opens the gate.
        self.app.upload_artifact_bytes(
            token=upload_token(submitted["figures"][0]["run"]),
            data=b"\x89PNG\r\n\x1a\n",
            kind="f",
        )
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertEqual(out["status"], "experiment_review")

    def test_report_size_ceiling(self) -> None:
        exp_id = self._drive_to_running_with_result()
        bloated = VALID_REPORT + "\n" + ("data row padding\n" * 1000)
        # Since byte capture landed (cloud plan Phase 1), the ceiling is
        # enforced at associate time — before the transition is ever attempted.
        with self.assertRaises(ValidationError) as ctx:
            self._submit(exp_id=exp_id, path="report.md", role="report", body=bloated)
        self.assertIn("bytes", str(ctx.exception))

    def test_workflow_surfaces_report_gate_after_results(self) -> None:
        exp_id = self._drive_to_running_with_result()
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "results_report_required")
        self.assertEqual(workflow["next_action"], "write_and_submit_results_report")
        self.assertEqual(workflow["artifact_guidance"]["role"], "report")
        self.assertIn("experiments/exp-1/report.md", workflow["artifact_guidance"]["guidance"])

    # ---- logic graph gate ----

    def test_submit_results_requires_logic_graph_resource(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertIn("logic graph", str(ctx.exception))
        self.assertIn("graph-template", str(ctx.exception))
        # Adding a valid graph unblocks the same transition.
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertEqual(out["status"], "experiment_review")

    def test_multiple_result_submissions_all_reach_the_attempt(self) -> None:
        exp_id = self._drive_to_running_with_result()
        for path in ("results/a.json", "results/b.json"):
            self._submit(exp_id=exp_id, path=path, role="result", body='{"metric": 1}\n')
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        submitted = {
            item["path"]
            for item in state["current_attempt_artifacts"]
            if item["role"] == "result"
        }
        self.assertIn("results/a.json", submitted)
        self.assertIn("results/b.json", submitted)

    def test_logic_graph_node_budget_is_enforced(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        nodes = ",".join(
            f'{{"id": "n{i}", "label": "step {i}"}}' for i in range(17)
        )
        over_budget = f'{{"version": 1, "nodes": [{nodes}]}}'
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=over_budget)
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        msg = str(ctx.exception)
        self.assertIn("17 nodes", msg)
        self.assertIn("16", msg)
        # The lint reads the submitted bytes: re-associating the fix unblocks.
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertEqual(out["status"], "experiment_review")

    def test_logic_graph_must_be_a_dag(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        cyclic = (
            '{"version": 1, "nodes": ['
            '{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],'
            ' "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}'
        )
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=cyclic)
        with self.assertRaises(WorkflowError) as ctx:
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self.assertIn("cycle", str(ctx.exception))

    def test_workflow_surfaces_graph_gate_after_report(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "logic_graph_required")
        self.assertEqual(workflow["next_action"], "write_and_submit_logic_graph")
        self.assertEqual(workflow["artifact_guidance"]["role"], "graph")
        self.assertIn("experiments/exp-1/graph.json", workflow["artifact_guidance"]["guidance"])

    def test_missing_graph_guidance_preserves_earlier_invalid_report_error(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(
            exp_id=exp_id,
            path="report.md",
            role="report",
            body="## Summary\nIncomplete report.\n",
        )
        state = self.call(
            "experiment.get_state",
            project_id=self.project_id,
            experiment_id=exp_id,
        )
        items = {item["id"]: item for item in state["gate_checklist"]["items"]}
        report_error = items["artifact:report"]["problems"][0]

        workflow = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=exp_id,
        )["workflow"]
        self.assertEqual(workflow["current_gate"], "logic_graph_required")
        self.assertEqual(workflow["artifact_guidance"]["role"], "graph")
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=exp_id,
                transition="submit_results",
            )
        self.assertEqual(str(ctx.exception), report_error)

    # ---- readiness pre-lint (status_and_next runs the deep lints) ----

    def test_ready_guidance_pre_lints_the_graph(self) -> None:
        exp_id = self._drive_to_running_with_result()
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        nodes = ",".join(f'{{"id": "n{i}", "label": "step {i}"}}' for i in range(17))
        over_budget = f'{{"version": 1, "nodes": [{nodes}]}}'
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=over_budget)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        graph_item = {
            item["id"]: item for item in state["gate_checklist"]["items"]
        }["artifact:graph"]
        self.assertEqual(graph_item["status"], "invalid")
        self.assertTrue(any("17 nodes" in p for p in graph_item["problems"]))
        self.assertFalse(state["gate_checklist"]["ready"])
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf.get("workflow") or wf
        # The workflow never says "submit" while the live graph would be rejected.
        self.assertEqual(workflow["current_gate"], "graph_invalid")
        self.assertEqual(workflow["next_action"], "fix_graph_artifact")
        self.assertTrue(any("17 nodes" in p for p in workflow["missing_evidence"]))
        self.assertEqual(workflow["artifact_guidance"]["role"], "graph")
        # Fixing the live file alone changes nothing (the gate lints submitted
        # bytes); re-associating the fix flips the guidance to ready.
        (self.repo / "graph.json").write_text(VALID_GRAPH)
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "graph_invalid")
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        graph_item = {
            item["id"]: item for item in state["gate_checklist"]["items"]
        }["artifact:graph"]
        self.assertEqual(graph_item["status"], "valid")
        self.assertTrue(state["gate_checklist"]["ready"])
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "experiment_review_required")

    def test_ready_guidance_pre_lints_the_plan(self) -> None:
        exp = self.call("experiment.create", name="thin-plan", project_id=self.project_id, intent="Thin plan.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body="loose notes\n")
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp["id"])
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["current_gate"], "plan_invalid")
        self.assertEqual(workflow["next_action"], "fix_plan_artifact")
        self.assertTrue(any("missing required sections" in p for p in workflow["missing_evidence"]))

    def test_pending_review_allows_fresh_request_for_lost_capability(self) -> None:
        exp = self.call("experiment.create", name="review-pending", project_id=self.project_id, intent="Review pending.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )

        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        review_item = state["gate_checklist"]["items"][0]
        self.assertEqual(review_item["id"], "review:design_reviewer")
        self.assertEqual(review_item["status"], "requested")
        self.assertFalse(review_item["satisfied"])
        review_status = self.call(
            "review.status",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
        )
        recovery = review_status["requests"][0]["recovery"]
        self.assertTrue(recovery["can_request_fresh_capability"])
        self.assertEqual(recovery["tool"], "review.request")
        self.assertEqual(recovery["arguments"]["role"], "design_reviewer")
        wf = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp["id"])
        workflow = wf.get("workflow") or wf
        self.assertEqual(workflow["review_gate"]["status"], "requested")
        self.assertIn("workflow.status_and_next", workflow["allowed_actions"])
        self.assertIn("review.request", workflow["allowed_actions"])
        self.assertNotIn("review.status", workflow["allowed_actions"])
        self._pass_review(exp_id=exp["id"], role="design_reviewer")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        review_item = state["gate_checklist"]["items"][0]
        self.assertEqual(review_item["status"], "passed")
        self.assertTrue(review_item["satisfied"])

    def test_claim_status_inference_is_negation_safe(self) -> None:
        infer = infer_claim_status_from_conclusion
        # Negative phrasings must never read as their positive stems.
        self.assertEqual(infer("The claim is unsupported by the data."), "weakened")
        self.assertEqual(infer("We could not confirm the hypothesis."), "weakened")
        self.assertEqual(infer("The new model was beaten by the baseline."), "weakened")
        self.assertEqual(infer("Accuracy did not improve over the baseline."), "weakened")
        # Unclear or off-topic conclusions return None instead of a guess.
        self.assertIsNone(infer("Results do not contradict the claim."))
        self.assertIsNone(infer("We failed to refute the null."))
        self.assertIsNone(infer("We mixed the two datasets before training."))
        self.assertIsNone(infer("The run finished; see the report for details."))
        # Conflicting directions bail rather than picking a tier.
        self.assertIsNone(
            infer("Metric A improved, however metric B contradicts the claim.")
        )
        # Clear directions still infer.
        self.assertEqual(
            infer("The threshold rule beat the baseline; the claim is supported."),
            "supported",
        )
        self.assertEqual(infer("Results were inconclusive."), "weakened")
        self.assertEqual(infer("The experiment refutes the claim."), "contradicted")

    def test_no_suggestion_without_an_inferable_status(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="Thresholding beats the majority baseline.",
        )
        self._drive_to_complete_with_claims(
            claim_ids=[claim["id"]],
            conclusion="The run finished; see the report for details.",
        )

    def _drive_to_complete_with_claims(
        self, *, claim_ids: list[str], conclusion: str
    ) -> None:
        exp = self.call(
            "experiment.create",
            name="no-suggestion",
            project_id=self.project_id,
            intent="No inferable status.",
            tested_claim_ids=claim_ids,
        )
        exp_id = exp["id"]
        self._submit(exp_id=exp_id, path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_ready_to_run")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="start_running")
        self._submit(exp_id=exp_id, path="results.json", role="result", body='{"metric": 1}\n')
        self._submit(exp_id=exp_id, path="report.md", role="report", body=VALID_REPORT)
        self._submit(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="complete",
            evidence={"conclusion": conclusion},
        )
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state.get("claim_update_suggestions", []), [])

    def test_fresh_capability_revokes_the_open_request(self) -> None:
        exp = self.call("experiment.create", name="revoke-on-refresh", project_id=self.project_id, intent="Revoke on refresh.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        old = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )
        fresh = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )
        # The superseded capability can no longer open a session.
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.start",
                review_request_id=old["review_request_id"],
                reviewer_capability=old["reviewer_capability"],
                caller_session_id="stale-reviewer",
            )
        session = self.call(
            "review.start",
            review_request_id=fresh["review_request_id"],
            reviewer_capability=fresh["reviewer_capability"],
            caller_session_id="fresh-reviewer",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The fresh reviewer capability passed cleanly after the stale one was superseded.",
        )
        # After the gate passed, review.status no longer advertises a refresh.
        status = self.call(
            "review.status",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
        )
        self.assertFalse(
            any(r["recovery"]["can_request_fresh_capability"] for r in status["requests"])
        )

    def test_stale_review_session_cannot_yank_an_advanced_experiment(self) -> None:
        exp = self.call("experiment.create", name="stale-session", project_id=self.project_id, intent="Stale session.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        old = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )
        stale_session = self.call(
            "review.start",
            review_request_id=old["review_request_id"],
            reviewer_capability=old["reviewer_capability"],
            caller_session_id="reviewer-a",
        )
        # A fresh request supersedes the started one; its reviewer passes the
        # gate and the experiment advances to running.
        self._pass_review(exp_id=exp["id"], role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="mark_ready_to_run")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="start_running")
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.submit",
                review_session_id=stale_session["review_session_id"],
                verdict="needs_changes",
                synopsis="This stale session should not be able to affect the experiment.",
                notes="stale objection",
            )
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["attempt_index"], 1)

    def test_send_back_to_planned_requires_a_review_status(self) -> None:
        exp_id = self._drive_to_running()
        with self.app.store.transaction() as conn:
            with self.assertRaises(WorkflowError):
                self.app.experiments.send_back_to_planned(
                    conn=conn,
                    experiment_id=exp_id,
                    revision_context="should not apply",
                )

    def test_review_request_returns_a_spawn_ready_handoff(self) -> None:
        exp = self.call("experiment.create", name="review-helper", project_id=self.project_id, intent="Review helper.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")

        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )
        handoff = req["reviewer_handoff"]
        self.assertEqual(handoff["skill"], "experiment-design-review")
        self.assertEqual(handoff["start_tool"], "review.start")
        # The spawn prompt carries everything the reviewer subagent needs to
        # start on its own — the requesting session never opens the session.
        self.assertIn(req["review_request_id"], handoff["spawn_prompt"])
        self.assertIn(req["reviewer_capability"], handoff["spawn_prompt"])
        self.assertIn("experiment-design-review", handoff["spawn_prompt"])

    def test_review_session_cannot_be_opened_by_the_producer(self) -> None:
        exp = self.call("experiment.create", name="review-helper-bad", project_id=self.project_id, intent="Review helper.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
            producer_session_id="same-session",
        )
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.start",
                review_request_id=req["review_request_id"],
                reviewer_capability=req["reviewer_capability"],
                caller_session_id="same-session",
            )

    # ---- synopsis validation ----

    def test_review_submit_rejects_too_short_synopsis(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                synopsis="Too short.",
            )
        self.assertIn("researcher's TLDR", str(ctx.exception))

    def test_review_submit_rejects_too_long_synopsis(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                synopsis="x" * 421,
            )
        self.assertIn("researcher's TLDR", str(ctx.exception))

    def test_review_submit_rejects_newline_in_synopsis(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                synopsis="The plan and results check out,\nso the attempt stands as reported today.",
            )
        self.assertIn("newline", str(ctx.exception))

    def test_review_submit_rejects_entity_id_in_synopsis(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                synopsis="Attempt exp_abc123 beat baseline handily according to the numbers observed.",
            )
        self.assertIn("entity ids", str(ctx.exception))

    def test_review_submit_rejects_backticks_in_synopsis(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                synopsis="The `threshold rule` beats the majority-class baseline by a solid margin.",
            )
        self.assertIn("backticks", str(ctx.exception))

    def test_review_submit_persists_synopsis_on_acceptance(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        synopsis = "The threshold rule clears the majority-class baseline, so the design is sound."
        out = self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis=synopsis,
        )
        self.assertEqual(out["synopsis"], synopsis)
        status = self.call(
            "review.status", project_id=self.project_id, target_type="experiment", target_id=exp_id
        )
        self.assertEqual(status["reviews"][0]["synopsis"], synopsis)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["reviews"][0]["synopsis"], synopsis)

    # ---- review rejection routing (return_to) ----

    def test_experiment_review_rejection_requires_return_to(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="needs_changes",
                synopsis="The conclusion overreaches what the retained metrics actually show.",
            )
        self.assertIn("return_to", str(ctx.exception))
        # The rejection was rolled back: nothing moved and the session stays
        # open, so the reviewer can resubmit with a routing decision.
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "experiment_review")
        out = self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="running",
            synopsis="The conclusion overreaches what the retained metrics actually show.",
        )
        self.assertEqual(out["return_to"], "running")

    def test_rejection_to_running_keeps_attempt_and_skips_design_review(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="running",
            synopsis="The conclusion overreaches what the retained metrics actually show.",
            notes="Conclusion overreaches; re-derive it from the retained metrics.",
        )
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["attempt_index"], 1)  # plan + resources stay valid
        self.assertIn("plan stands", state["revision_context"])
        self.assertIn("Conclusion overreaches", state["revision_context"])
        # Soft reminder only — "Consider updating", not a directive: keeping the
        # logic graph current is the agent's editorial call.
        self.assertIn("Consider updating the experiment's logic graph", state["revision_context"])
        # The fix loop: update results and resubmit — no new plan, no new
        # design review — then a passing review completes the experiment.
        (self.repo / "results.json").write_text("{\"metric\": 2}\n")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="complete")
        self.assertEqual(out["status"], "complete")

    def test_rejection_to_planned_advances_attempt(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="fail",
            return_to="planned",
            synopsis="The plan itself does not test the claim, so this needs a fresh design.",
        )
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "planned")
        self.assertEqual(state["attempt_index"], 2)

    def test_design_review_rejection_cannot_return_to_running(self) -> None:
        exp = self.call("experiment.create", name="exp-9", project_id=self.project_id, intent="Design reject.")
        self._submit(exp_id=exp["id"], path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        session_id = self._open_review_session(exp_id=exp["id"], role="design_reviewer")
        with self.assertRaises(ValidationError):
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="needs_changes",
                return_to="running",
                synopsis="A design rejection cannot skip straight back to running.",
            )
        # Without return_to a design rejection still routes to planned.
        out = self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            synopsis="The design does not yet test the claim as scoped, so it needs revision.",
        )
        self.assertEqual(out["return_to"], "planned")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])
        self.assertEqual(state["status"], "planned")
        self.assertEqual(state["attempt_index"], 2)

    def test_pass_verdict_rejects_return_to(self) -> None:
        exp_id = self._drive_to_experiment_review()
        session_id = self._open_review_session(exp_id=exp_id, role="experiment_reviewer")
        with self.assertRaises(ValidationError):
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="pass",
                return_to="running",
                synopsis="A passing verdict should not also carry a return_to.",
            )

    # ---- claim.update ----

    def test_claim_update_changes_status_and_confidence(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Bigger batch helps.")
        self.assertEqual(claim["status"], "active")
        updated = self.call(
            "claim.update",
            project_id=self.project_id,
            claim_id=claim["id"],
            status="supported",
            confidence="high",
        )
        self.assertEqual(updated["status"], "supported")
        self.assertEqual(updated["confidence"], "high")
        self.assertEqual(updated["statement"], "Bigger batch helps.")

    def test_claim_update_rejects_unknown_status(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        with self.assertRaises(ValidationError):
            self.call("claim.update", project_id=self.project_id, claim_id=claim["id"], status="bogus")

    def test_claim_update_is_project_scoped(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        other = self.call("project", action="create", name="Other")
        from merv.brain.kernel.utils import NotFoundError

        with self.assertRaises(NotFoundError):
            self.call("claim.update", project_id=other["id"], claim_id=claim["id"], status="supported")

    def test_claim_update_rejects_statement_and_scope_rewrites(self) -> None:
        # The statement is the claim's identity: experiments and reviews
        # reference the claim id assuming stable meaning. Text revisions go
        # through a reviewed reflection change spec or abandon-and-recreate.
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        with self.assertRaises(ValidationError):
            self.call(
                "claim.update",
                project_id=self.project_id,
                claim_id=claim["id"],
                statement="Rewritten.",
            )
        with self.assertRaises(ValidationError):
            self.call(
                "claim.update",
                project_id=self.project_id,
                claim_id=claim["id"],
                scope="widened",
            )

    def test_claim_update_requires_status_or_confidence(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        with self.assertRaises(ValidationError):
            self.call("claim.update", project_id=self.project_id, claim_id=claim["id"])

    def test_claim_events_carry_full_post_state(self) -> None:
        # Claim events double as the claim's version history (there is no
        # claim_versions table): created and every update must record the
        # complete post-state.
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="Bigger batch helps.",
            scope="toy runs only",
        )
        self.call(
            "claim.update",
            project_id=self.project_id,
            claim_id=claim["id"],
            status="supported",
        )
        conn = self.app.store.connect()
        try:
            rows = conn.execute(
                "SELECT type, payload_json FROM events"
                " WHERE target_type = 'claim' AND target_id = ? ORDER BY id",
                (claim["id"],),
            ).fetchall()
        finally:
            conn.close()
        payloads = {row["type"]: json.loads(row["payload_json"]) for row in rows}
        self.assertEqual(
            payloads["claim.created"],
            {
                "statement": "Bigger batch helps.",
                "scope": "toy runs only",
                "status": "active",
                "confidence": "medium",
            },
        )
        self.assertEqual(
            payloads["claim.updated"],
            {
                "statement": "Bigger batch helps.",
                "scope": "toy runs only",
                "status": "supported",
                "confidence": "medium",
            },
        )


if __name__ == "__main__":
    unittest.main()
