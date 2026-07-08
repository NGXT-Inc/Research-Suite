from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.domain.experiment_policy import ACTIVE_EXPERIMENT_CAP
from backend.domain.reflection_policy import (
    REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
)
from backend.utils import PermissionDeniedError, ValidationError, WorkflowError

# A project logic graph that satisfies the envelope lint (valid JSON, ≤16
# nodes, DAG) — the same lint experiments use, one level up.
VALID_PROJECT_GRAPH = (
    '{"version": 1, "title": "Project logic", "nodes": ['
    '{"id": "lesson", "kind": "lesson", "label": "LR schedule dominates"},'
    '{"id": "open", "kind": "open_question", "label": "Does it hold at scale?"}],'
    ' "edges": [{"from": "lesson", "to": "open", "label": "raises"}]}\n'
)

REVISED_PROJECT_GRAPH = (
    '{"version": 1, "title": "Project logic", "nodes": ['
    '{"id": "lesson", "kind": "lesson", "label": "LR schedule is conditional"},'
    '{"id": "scale", "kind": "open_question", "label": "Scale is the discriminator"}],'
    ' "edges": [{"from": "lesson", "to": "scale", "label": "prioritizes"}]}\n'
)

VALID_REFLECTION_DOC = (
    "# Synthesis\n\n"
    "## Summary\n"
    "The wave reconciles the lens reflections into the current project state.\n\n"
    "## Critical reading\n"
    "The LR-schedule direction remains live, while prior optimizer swaps are compressed as dead ends.\n\n"
    "## Decision / future directions\n"
    "Create a small parallel wave to test transfer and mechanism questions.\n"
)

VALID_CHANGE_SPEC = json.dumps(
    {
        "version": 1,
        "claim_changes": [
            {
                "op": "create",
                "key": "claim_schedule_transfer",
                "statement": "The LR-schedule effect transfers at larger scale.",
                "scope": "Toy gate-test project.",
                "confidence": "medium",
                "rationale": "The reflection wave surfaced this as the next belief to test.",
            }
        ],
        "decision": {
            "type": "create_experiments",
            "experiments": [
                {
                    "key": "scale_check",
                    "name": "scale-check",
                    "intent": "Test whether the LR-schedule effect transfers at larger scale.",
                    "tested_claim_refs": ["claim_schedule_transfer"],
                    "parallelism": "Independent scale axis; can run beside mechanism-probe.",
                },
                {
                    "key": "mechanism_probe",
                    "name": "mechanism-probe",
                    "intent": "Probe whether clipping interaction explains the LR-schedule effect.",
                    "tested_claim_refs": ["claim_schedule_transfer"],
                    "parallelism": "Independent mechanism axis; no dependency on scale-check.",
                },
            ],
        },
    }
)

# A full 5-lens roster: the three core lenses plus two wave-authored ones,
# each with a charter and a stated distinctness reason.
def full_roster() -> list[dict[str, str]]:
    return [
        {"id": "amplify"},
        {"id": "avoid"},
        {"id": "entropy"},
        {
            "id": "rigor",
            "charter": "Methodological soundness of the experiments.",
            "why_distinct": "Judges how we measured, not what we found or skipped.",
        },
        {
            "id": "cost",
            "charter": "Compute spent vs information gained per experiment.",
            "why_distinct": "Prices the exploration; no core lens does.",
        },
    ]


ALL_LENS_IDS = ("amplify", "avoid", "entropy", "rigor", "cost")


class SynthesisGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Synthesis Gate Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _create_wave(self, *, title: str = "Wave") -> str:
        return self.call(
            "reflection.create", project_id=self.project_id, title=title, lenses=full_roster()
        )["id"]

    def _associate_file(self, *, syn_id: str, path: str, role: str, body: str) -> str:
        full = self.repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
        res = self.call("resource.register_file", project_id=self.project_id, path=path)
        self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=res["id"],
            target_type="reflection",
            target_id=syn_id,
            role=role,
        )
        return res["id"]

    def _submit_reflection(self, *, syn_id: str, lens_id: str) -> None:
        self._associate_file(
            syn_id=syn_id,
            path=f"syntheses/{syn_id}/reflections/{lens_id}.md",
            role="reflection_lens_doc",
            body=f"# {lens_id}\nFindings through the {lens_id} lens.\n",
        )

    def _create_active_experiments(self, count: int) -> None:
        for index in range(count):
            self.call(
                "experiment.create",
                name=f"active-{index}",
                project_id=self.project_id,
                intent="Keep this experiment active.",
            )

    def _drive_to_synthesizing(self) -> str:
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflections",
        )
        return syn_id

    def _associate_synthesis_artifacts(
        self,
        *,
        syn_id: str,
        graph: str = VALID_PROJECT_GRAPH,
        doc: str = VALID_REFLECTION_DOC,
        change_spec: str = VALID_CHANGE_SPEC,
    ) -> None:
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=graph
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=doc
        )
        self._associate_file(
            syn_id=syn_id,
            path="project/change_spec.json",
            role="change_spec",
            body=change_spec,
        )

    def _drive_to_synthesis_review(self) -> str:
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id)
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        return syn_id

    def _open_review_session(self, *, syn_id: str, caller: str = "synthesis-reviewer") -> str:
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_reviewer",
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=caller,
        )
        return session["review_session_id"]

    def _drive_to_published(self) -> str:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="publish",
        )
        return syn_id

    def _state(self, syn_id: str) -> dict:
        return self.call("reflection.get", project_id=self.project_id, reflection_id=syn_id)

    # ---- roster envelope ----

    def test_roster_must_be_exactly_five_lenses(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=[])
        self.assertIn("exactly 5 lenses", str(ctx.exception))
        with self.assertRaises(ValidationError):
            self.call(
                "reflection.create", project_id=self.project_id, lenses=full_roster()[:4]
            )

    def test_roster_requires_all_core_lenses(self) -> None:
        roster = full_roster()
        roster[0] = {
            "id": "vibes",
            "charter": "General vibes.",
            "why_distinct": "It is vibes.",
        }
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=roster)
        self.assertIn("missing core lens(es): amplify", str(ctx.exception))

    def test_authored_lenses_require_charter_and_why_distinct(self) -> None:
        roster = full_roster()
        roster[3] = {"id": "rigor", "charter": "Method soundness."}  # no why_distinct
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=roster)
        self.assertIn("why_distinct", str(ctx.exception))
        roster[3] = {"id": "rigor", "why_distinct": "Different."}  # no charter
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=roster)
        self.assertIn("charter", str(ctx.exception))

    def test_roster_rejects_duplicate_and_malformed_ids(self) -> None:
        roster = full_roster()
        roster[4] = dict(roster[3])
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=roster)
        self.assertIn("duplicate lens id", str(ctx.exception))
        roster = full_roster()
        roster[4]["id"] = "Not A Slug!"
        with self.assertRaises(ValidationError) as ctx:
            self.call("reflection.create", project_id=self.project_id, lenses=roster)
        self.assertIn("invalid lens id", str(ctx.exception))

    def test_core_lenses_default_their_charters(self) -> None:
        syn = self._state(self._create_wave())
        by_id = {lens["id"]: lens for lens in syn["roster"]}
        self.assertTrue(by_id["amplify"]["core"])
        self.assertIn("positive signal", by_id["amplify"]["charter"])
        self.assertIn("negative-knowledge ledger", by_id["avoid"]["charter"])
        self.assertFalse(by_id["rigor"]["core"])

    def test_reflection_tool_namespace_exposes_wave_tools(self) -> None:
        syn_id = self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Canonical namespace",
            lenses=full_roster(),
        )["id"]
        self.assertEqual(
            self.call(
                "reflection.get",
                project_id=self.project_id,
                reflection_id=syn_id,
            )["id"],
            syn_id,
        )
        listed = self.call("reflection.list", project_id=self.project_id)["reflections"]
        self.assertEqual([row["id"] for row in listed], [syn_id])
        self.assertEqual(
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="abandon",
            )["status"],
            "abandoned",
        )

    def test_only_one_wave_may_be_open(self) -> None:
        self._create_wave()
        with self.assertRaises(WorkflowError) as ctx:
            self._create_wave(title="Second")
        self.assertIn("already open", str(ctx.exception))
        # A terminal wave (here: abandoned) frees the slot.
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=self._state_id_of_open_wave(),
            transition="abandon",
        )
        self._create_wave(title="Third")

    def _state_id_of_open_wave(self) -> str:
        syntheses = self.call("reflection.list", project_id=self.project_id)["reflections"]
        open_waves = [
            syn for syn in syntheses if syn["status"] not in {"published", "abandoned"}
        ]
        self.assertEqual(len(open_waves), 1)
        return open_waves[0]["id"]

    # ---- roster coverage gate (the hard 'all 5 before synthesize' rule) ----

    def test_submit_reflections_blocked_until_every_lens_submits(self) -> None:
        syn_id = self._create_wave()
        with self.assertRaises(WorkflowError):
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflections",
            )
        # Four of five: the error names exactly the missing lens.
        for lens_id in ALL_LENS_IDS[:-1]:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("cost", str(ctx.exception))
        self.assertNotIn("amplify,", str(ctx.exception))
        self._submit_reflection(syn_id=syn_id, lens_id="cost")
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflections",
        )
        self.assertEqual(out["status"], "synthesizing")

    def test_reflection_must_be_named_after_its_lens(self) -> None:
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS[:-1]:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        # role 'reflection_lens_doc' but the filename stem matches no lens ⇒ not coverage.
        self._associate_file(
            syn_id=syn_id,
            path=f"syntheses/{syn_id}/reflections/notes.md",
            role="reflection_lens_doc",
            body="loose notes\n",
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("cost", str(ctx.exception))

    def test_empty_reflection_blocks_submit(self) -> None:
        # The gate lints the SUBMITTED bytes: a reflection whose associated
        # content is blank blocks the transition (and emptying a live file
        # after association would be invisible — submission is what counts).
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS:
            if lens_id == "rigor":
                self._associate_file(
                    syn_id=syn_id,
                    path=f"syntheses/{syn_id}/reflections/rigor.md",
                    role="reflection_lens_doc",
                    body="   \n",
                )
            else:
                self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("empty", str(ctx.exception))

    def test_get_state_reports_reflection_coverage(self) -> None:
        syn_id = self._create_wave()
        self._submit_reflection(syn_id=syn_id, lens_id="amplify")
        coverage = self._state(syn_id)["reflection_coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(
            set(coverage["missing"]), {"avoid", "entropy", "rigor", "cost"}
        )

    def test_gate_checklist_tracks_missing_reflection_lenses(self) -> None:
        syn_id = self._create_wave()
        self._submit_reflection(syn_id=syn_id, lens_id="amplify")

        checklist = self._state(syn_id)["gate_checklist"]
        self.assertEqual(checklist["status"], "reflecting")
        self.assertEqual(checklist["transition"], "submit_reflections")
        self.assertEqual(checklist["leads_to"], "synthesizing")
        self.assertFalse(checklist["ready"])
        items = {item["id"]: item for item in checklist["items"]}
        self.assertTrue(items["reflection_lens:amplify"]["satisfied"])
        self.assertEqual(items["reflection_lens:amplify"]["status"], "present")
        self.assertFalse(items["reflection_lens:cost"]["satisfied"])
        self.assertEqual(items["reflection_lens:cost"]["status"], "missing")
        self.assertIn("cost", items["reflection_lens:cost"]["missing"])

        for lens_id in ("avoid", "entropy", "rigor", "cost"):
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        checklist = self._state(syn_id)["gate_checklist"]
        self.assertTrue(checklist["ready"])
        self.assertTrue(all(item["satisfied"] for item in checklist["items"]))

    def test_legacy_reflection_role_is_rejected_for_new_associations(self) -> None:
        syn_id = self._create_wave()
        with self.assertRaises(ValidationError) as ctx:
            self._associate_file(
                syn_id=syn_id,
                path=f"syntheses/{syn_id}/reflections/amplify.md",
                role="reflection",
                body="# amplify\nLegacy role output.\n",
            )
        self.assertIn("legacy resource role 'reflection'", str(ctx.exception))

    # ---- synthesis artifacts gate ----

    def test_submit_reflection_artifacts_requires_graph_doc_then_change_spec(self) -> None:
        syn_id = self._drive_to_synthesizing()
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("project logic graph", str(ctx.exception))
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("reflection document", str(ctx.exception))
        self._associate_file(
            syn_id=syn_id,
            path="project/reflection.md",
            role="reflection_doc",
            body=VALID_REFLECTION_DOC,
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("change spec", str(ctx.exception))
        self._associate_file(
            syn_id=syn_id,
            path="project/change_spec.json",
            role="change_spec",
            body=VALID_CHANGE_SPEC,
        )
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        self.assertEqual(out["status"], "reflection_review")

    def test_gate_checklist_tracks_reflection_artifacts(self) -> None:
        syn_id = self._drive_to_synthesizing()

        checklist = self._state(syn_id)["gate_checklist"]
        self.assertEqual(checklist["status"], "synthesizing")
        self.assertEqual(checklist["transition"], "submit_reflection_artifacts")
        self.assertEqual(checklist["leads_to"], "reflection_review")
        self.assertFalse(checklist["ready"])
        items = {item["id"]: item for item in checklist["items"]}
        self.assertEqual(items["resource:project_graph"]["status"], "missing")

        self._associate_file(
            syn_id=syn_id,
            path="project/logic_graph.json",
            role="project_graph",
            body=VALID_PROJECT_GRAPH,
        )
        checklist = self._state(syn_id)["gate_checklist"]
        items = {item["id"]: item for item in checklist["items"]}
        self.assertEqual(items["resource:project_graph"]["status"], "valid")
        self.assertTrue(items["resource:project_graph"]["satisfied"])
        self.assertEqual(items["resource:reflection_doc"]["status"], "missing")

        self._associate_file(
            syn_id=syn_id,
            path="project/reflection.md",
            role="reflection_doc",
            body=VALID_REFLECTION_DOC,
        )
        self._associate_file(
            syn_id=syn_id,
            path="project/change_spec.json",
            role="change_spec",
            body="## old markdown change spec\n",
        )
        checklist = self._state(syn_id)["gate_checklist"]
        items = {item["id"]: item for item in checklist["items"]}
        self.assertEqual(items["resource:reflection_doc"]["status"], "valid")
        self.assertEqual(items["resource:change_spec"]["status"], "invalid")
        self.assertFalse(items["resource:change_spec"]["satisfied"])
        self.assertIn("not valid JSON", items["resource:change_spec"]["problems"][0])

        self._associate_file(
            syn_id=syn_id,
            path="project/change_spec.json",
            role="change_spec",
            body=VALID_CHANGE_SPEC,
        )
        checklist = self._state(syn_id)["gate_checklist"]
        self.assertTrue(checklist["ready"])
        self.assertTrue(all(item["satisfied"] for item in checklist["items"]))

    def test_legacy_synthesis_doc_role_is_rejected_for_new_associations(self) -> None:
        syn_id = self._drive_to_synthesizing()
        with self.assertRaises(ValidationError) as ctx:
            self._associate_file(
                syn_id=syn_id,
                path="project/synthesis.md",
                role="synthesis_doc",
                body=VALID_REFLECTION_DOC,
            )
        self.assertIn("legacy resource role 'synthesis_doc'", str(ctx.exception))

    def test_legacy_project_graph_role_is_rejected_for_reflection_waves(self) -> None:
        syn_id = self._drive_to_synthesizing()
        with self.assertRaises(ValidationError) as ctx:
            self._associate_file(
                syn_id=syn_id,
                path="project/logic_graph.json",
                role="graph",
                body=VALID_PROJECT_GRAPH,
            )
        self.assertIn("use role 'project_graph'", str(ctx.exception))

    def test_project_graph_over_budget_is_rejected_plainly(self) -> None:
        syn_id = self._drive_to_synthesizing()
        nodes = [{"id": f"n{i}", "label": f"Node {i}"} for i in range(17)]
        self._associate_file(
            syn_id=syn_id,
            path="project/logic_graph.json",
            role="project_graph",
            body=json.dumps({"version": 1, "nodes": nodes}),
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=VALID_REFLECTION_DOC
        )
        self._associate_file(
            syn_id=syn_id, path="project/change_spec.json", role="change_spec", body=VALID_CHANGE_SPEC
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        message = str(ctx.exception)
        self.assertIn("reduce the graph", message)
        self.assertNotIn("collapse", message)
        self.assertNotIn("merge", message)

    def test_empty_reflection_doc_is_rejected(self) -> None:
        syn_id = self._drive_to_synthesizing()
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body="  \n"
        )
        self._associate_file(
            syn_id=syn_id, path="project/change_spec.json", role="change_spec", body=VALID_CHANGE_SPEC
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("empty", str(ctx.exception))

    def test_reflection_doc_requires_critical_reading_section(self) -> None:
        syn_id = self._drive_to_synthesizing()
        doc = (
            "# Synthesis\n\n"
            "## Summary\nShort summary.\n\n"
            "## Decision / future directions\nCreate the approved wave.\n"
        )
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=doc
        )
        self._associate_file(
            syn_id=syn_id, path="project/change_spec.json", role="change_spec", body=VALID_CHANGE_SPEC
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("Critical reading", str(ctx.exception))

    def test_verbose_reflection_doc_is_rejected(self) -> None:
        syn_id = self._drive_to_synthesizing()
        verbose_doc = (
            "# Synthesis\n\n"
            "## Summary\n"
            "Short summary.\n\n"
            "## Critical reading\n"
            + ("This paragraph is intentionally too long for the reflection document. " * 260)
            + "\n\n## Decision / future directions\n"
            "Create the approved parallel wave.\n"
        )
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        with self.assertRaises(ValidationError) as ctx:
            self._associate_file(
                syn_id=syn_id,
                path="project/reflection.md",
                role="reflection_doc",
                body=verbose_doc,
            )
        self.assertIn("maximum", str(ctx.exception))

    def test_reflection_doc_requires_submitted_relative_images(self) -> None:
        syn_id = self._drive_to_synthesizing()
        doc = (
            "# Synthesis\n\n"
            "## Summary\nShort summary.\n\n"
            "![project graph](figures/project_graph.png)\n\n"
            "## Critical reading\nThe visual is needed for this reading.\n\n"
            "## Decision / future directions\nCreate the approved wave.\n"
        )
        # A dangling figure link is rejected at associate time, before any
        # bytes are pinned.
        with self.assertRaises(ValidationError) as ctx:
            self._associate_file(
                syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=doc
            )
        self.assertIn("figures/project_graph.png", str(ctx.exception))

    def test_gate_blocks_reflection_doc_with_unsubmitted_images(self) -> None:
        # Defense in depth: a non-compliant data plane could submit the doc
        # bytes without the figures the markdown links; the gate must block.
        syn_id = self._drive_to_synthesizing()
        doc = (
            "# Synthesis\n\n"
            "## Summary\nShort summary.\n\n"
            "![project graph](figures/project_graph.png)\n\n"
            "## Critical reading\nThe visual is needed for this reading.\n\n"
            "## Decision / future directions\nCreate the approved wave.\n"
        )
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/change_spec.json", role="change_spec", body=VALID_CHANGE_SPEC
        )
        full = self.repo / "project" / "reflection.md"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(doc)
        res = self.call("resource.register_file", project_id=self.project_id, path="project/reflection.md")
        self.app._control_api_post(
            "/api/data-plane/resources/associate",
            {
                "project_id": self.project_id,
                "resource_id": res["id"],
                "target_type": "reflection",
                "target_id": syn_id,
                "role": "reflection_doc",
                "blob": {
                    "data_b64": base64.b64encode(doc.encode()).decode("ascii"),
                    "content_type": "text/markdown",
                },
            },
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("image 'figures/project_graph.png' has no submitted content", str(ctx.exception))

    def test_reflection_doc_submits_relative_images(self) -> None:
        syn_id = self._drive_to_synthesizing()
        (self.repo / "project" / "figures").mkdir(parents=True, exist_ok=True)
        (self.repo / "project" / "figures" / "project_graph.png").write_bytes(
            b"\x89PNG\r\n\x1a\nfake"
        )
        doc = (
            "# Synthesis\n\n"
            "## Summary\nShort summary.\n\n"
            "![project graph](figures/project_graph.png)\n\n"
            "## Critical reading\nThe visual is submitted with the reflection doc.\n\n"
            "## Decision / future directions\nCreate the approved wave.\n"
        )
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=doc
        )
        self._associate_file(
            syn_id=syn_id, path="project/change_spec.json", role="change_spec", body=VALID_CHANGE_SPEC
        )
        with self.app.store.connect() as conn:
            row = conn.execute(
                "SELECT sha256 FROM report_figures WHERE link_path = ?",
                ("figures/project_graph.png",),
            ).fetchone()
        self.assertIsNotNone(row)
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        self.assertEqual(out["status"], "reflection_review")

    def test_change_spec_must_be_materializable(self) -> None:
        syn_id = self._drive_to_synthesizing()
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="project_graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/reflection.md", role="reflection_doc", body=VALID_REFLECTION_DOC
        )
        self._associate_file(
            syn_id=syn_id,
            path="project/change_spec.json",
            role="change_spec",
            body="## old markdown change spec\n",
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_change_spec_cannot_exceed_active_experiment_cap(self) -> None:
        self._create_active_experiments(ACTIVE_EXPERIMENT_CAP - 1)
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id)
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        message = str(ctx.exception)
        self.assertIn("active experiment cap would be exceeded", message)
        self.assertIn("finish one before creating another", message)

    def test_publish_materializes_claim_changes_and_experiment_wave(self) -> None:
        existing = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="Schedule effect appears local.",
        )
        outcome = {
            "version": 1,
            "claim_changes": [
                {
                    "op": "update",
                    "claim_id": existing["id"],
                    "status": "supported",
                    "confidence": "high",
                    "rationale": "The published synthesis reconciles the evidence.",
                },
                {
                    "op": "create",
                    "key": "claim_transfer",
                    "statement": "Schedule effect transfers across scale.",
                    "confidence": "medium",
                    "rationale": "This is the next belief to test.",
                },
            ],
            "decision": {
                "type": "create_experiments",
                "experiments": [
                    {
                        "key": "scale",
                        "name": "scale-transfer",
                        "intent": "Test scale transfer.",
                        "tested_claim_refs": ["claim_transfer"],
                        "parallelism": "Independent scale axis.",
                    },
                    {
                        "key": "data",
                        "name": "data-transfer",
                        "intent": "Test data transfer.",
                        "tested_claim_refs": ["claim_transfer"],
                        "parallelism": "Independent data axis.",
                    },
                ],
            },
        }
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id, change_spec=json.dumps(outcome))
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        published = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="publish",
        )
        self.assertEqual(published["status"], "published")
        guidance = published["post_publish_guidance"]
        self.assertIn("created 2 planned experiments", guidance["summary"])
        self.assertEqual(
            {exp["folder"] for exp in guidance["experiments"]},
            {"experiments/scale-transfer/", "experiments/data-transfer/"},
        )
        self.assertEqual(
            guidance["recommended_actions"][0],
            {
                "tool": "experiment.materialize_folders",
                "arguments": {"status": "planned"},
                "why": "Create local folders for the newly planned experiment wave.",
            },
        )
        self.assertEqual(
            guidance["recommended_actions"][1]["tool"],
            "workflow.status_and_next",
        )
        claims = self.call("claim.list", project_id=self.project_id)["claims"]
        by_statement = {claim["statement"]: claim for claim in claims}
        self.assertEqual(by_statement["Schedule effect appears local."]["status"], "supported")
        created_claim = by_statement["Schedule effect transfers across scale."]
        experiments = self.call("experiment.list", project_id=self.project_id)["experiments"]
        self.assertEqual({exp["name"] for exp in experiments}, {"scale-transfer", "data-transfer"})
        for exp in experiments:
            self.assertEqual(exp["status"], "planned")
            self.assertEqual([claim["id"] for claim in exp["tested_claims"]], [created_claim["id"]])
        detail = self._state(syn_id)
        self.assertEqual(len(detail["materialized_claims"]), 2)
        self.assertEqual(len(detail["materialized_experiments"]), 2)
        self.assertEqual(detail["post_publish_guidance"], guidance)

    def test_reflection_state_diffs_project_graph_against_previous_publish(self) -> None:
        first_syn_id = self._drive_to_published()
        second_syn_id = self._drive_to_synthesizing()
        self._associate_file(
            syn_id=second_syn_id,
            path="project/logic_graph.json",
            role="project_graph",
            body=REVISED_PROJECT_GRAPH,
        )

        diff = self._state(second_syn_id)["project_graph_diff"]
        self.assertTrue(diff["available"])
        self.assertEqual(diff["base_reflection_id"], first_syn_id)
        self.assertEqual(diff["current_reflection_id"], second_syn_id)
        self.assertTrue(diff["base_graph_version_id"])
        self.assertTrue(diff["current_graph_version_id"])
        self.assertIn("1 nodes added", diff["summary"])
        self.assertEqual(
            [node["id"] for node in diff["nodes"]["added"]],
            ["scale"],
        )
        self.assertEqual(
            [node["id"] for node in diff["nodes"]["removed"]],
            ["open"],
        )
        self.assertEqual(
            [(item["id"], item["changed_fields"]) for item in diff["nodes"]["changed"]],
            [("lesson", ["label"])],
        )
        self.assertEqual(
            [edge["to"] for edge in diff["edges"]["added"]],
            ["scale"],
        )
        self.assertEqual(
            [edge["to"] for edge in diff["edges"]["removed"]],
            ["open"],
        )

    def test_publish_defensively_rechecks_active_experiment_cap(self) -> None:
        self._create_active_experiments(ACTIVE_EXPERIMENT_CAP - 2)
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        self.call(
            "experiment.create",
            name="late-active-a",
            project_id=self.project_id,
            intent="Created after reflection review.",
        )
        self.call(
            "experiment.create",
            name="late-active-b",
            project_id=self.project_id,
            intent="Created after reflection review.",
        )

        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="publish",
            )
        message = str(ctx.exception)
        self.assertIn("active experiment cap would be exceeded", message)
        self.assertIn("finish one before creating another", message)

    def test_publish_claim_update_null_status_confidence_preserves_existing(self) -> None:
        existing = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="Schedule effect survives null updates.",
            confidence="high",
        )
        # A single-experiment wave also exercises the relaxed decision rule:
        # one experiment is enough, and it needs no parallelism note.
        outcome = {
            "version": 1,
            "claim_changes": [
                {
                    "op": "update",
                    "claim_id": existing["id"],
                    "status": None,
                    "confidence": None,
                    "rationale": "A null update should preserve existing values.",
                }
            ],
            "decision": {
                "type": "create_experiments",
                "experiments": [
                    {
                        "key": "null_update_check",
                        "name": "null-update-check",
                        "intent": "Confirm the schedule effect after the null update.",
                        "tested_claim_refs": [existing["id"]],
                    }
                ],
            },
        }
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id, change_spec=json.dumps(outcome))
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="publish",
        )

        claims = self.call("claim.list", project_id=self.project_id)["claims"]
        [claim] = [claim for claim in claims if claim["id"] == existing["id"]]
        self.assertEqual(claim["status"], "active")
        self.assertEqual(claim["confidence"], "high")

    def test_rejected_synthesis_does_not_materialize_change_spec(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="synthesizing",
            synopsis="The change spec is not yet justified by the reviewed reflections.",
            notes="change spec is not justified yet",
        )
        self.assertEqual(self.call("claim.list", project_id=self.project_id)["claims"], [])
        self.assertEqual(self.call("experiment.list", project_id=self.project_id)["experiments"], [])

    def test_change_spec_rejects_hard_stop_decision(self) -> None:
        # Stopping the project is the researcher's call — the old hard_stop
        # decision must not validate.
        outcome = {
            "version": 1,
            "claim_changes": [],
            "decision": {
                "type": "hard_stop",
                "rationale": "All viable directions are ruled out by reviewed dead ends.",
            },
        }
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id, change_spec=json.dumps(outcome))
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("decision.type must be 'create_experiments'", str(ctx.exception))

    def test_multi_experiment_wave_requires_parallelism_notes(self) -> None:
        spec = json.loads(VALID_CHANGE_SPEC)
        del spec["decision"]["experiments"][1]["parallelism"]
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id, change_spec=json.dumps(spec))
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn(
            "parallelism is required for a multi-experiment wave", str(ctx.exception)
        )

    def test_change_spec_requires_at_least_one_experiment(self) -> None:
        spec = json.loads(VALID_CHANGE_SPEC)
        spec["decision"]["experiments"] = []
        syn_id = self._drive_to_synthesizing()
        self._associate_synthesis_artifacts(syn_id=syn_id, change_spec=json.dumps(spec))
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflection_artifacts",
            )
        self.assertIn("at least one experiment", str(ctx.exception))

    # ---- review gate + routing ----

    def test_publish_requires_a_passing_synthesis_review(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        with self.assertRaises(WorkflowError):
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="publish",
            )
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="publish",
        )
        self.assertEqual(out["status"], "published")
        self.assertTrue(out["published_at"])
        self.assertTrue(out["published_graph_version_id"])

    def test_gate_checklist_tracks_reflection_review(self) -> None:
        syn_id = self._drive_to_synthesis_review()

        checklist = self._state(syn_id)["gate_checklist"]
        self.assertEqual(checklist["status"], "reflection_review")
        self.assertEqual(checklist["transition"], "publish")
        self.assertFalse(checklist["ready"])
        review_item = checklist["items"][0]
        self.assertEqual(review_item["id"], "review:reflection_reviewer")
        self.assertEqual(review_item["status"], "pending")

        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_reviewer",
        )
        checklist = self._state(syn_id)["gate_checklist"]
        review_item = checklist["items"][0]
        self.assertEqual(review_item["status"], "requested")
        self.assertEqual(review_item["request_id"], req["review_request_id"])

        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="reflection-reviewer",
        )
        checklist = self._state(syn_id)["gate_checklist"]
        self.assertEqual(checklist["items"][0]["status"], "started")

        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        checklist = self._state(syn_id)["gate_checklist"]
        self.assertTrue(checklist["ready"])
        self.assertEqual(checklist["items"][0]["status"], "passed")
        self.assertEqual(checklist["items"][0]["action"], "publish_reflection")

    def test_review_request_role_must_match_the_synthesis_gate(self) -> None:
        syn_id = self._create_wave()
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.request",
                project_id=self.project_id,
                target_type="reflection",
                target_id=syn_id,
                role="reflection_reviewer",
            )
        # Only one wave may be open, so drive this same one forward.
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflections",
        )
        self._associate_synthesis_artifacts(syn_id=syn_id)
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.call(
                "review.request",
                project_id=self.project_id,
                target_type="reflection",
                target_id=syn_id,
                role="experiment_reviewer",
            )
        self.assertIn("reflection_reviewer", str(ctx.exception))

    def test_synthesis_rejection_requires_explicit_return_to(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="needs_changes",
                synopsis="A pattern in the dead-ends ledger is missing from the reflection.",
                notes="missing dead ends",
            )
        self.assertIn("reflecting", str(ctx.exception))
        self.assertIn("synthesizing", str(ctx.exception))
        # Experiment return targets are invalid for a synthesis review.
        with self.assertRaises(ValidationError):
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="needs_changes",
                return_to="planned",
                synopsis="A pattern in the dead-ends ledger is missing from the reflection.",
            )

    def test_redo_synthesis_keeps_reflections_and_attempt(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="synthesizing",
            synopsis="A dead end is retold as a near-win, so the reflection artifacts need revision.",
            notes="a dead end is retold as a near-win",
        )
        state = self._state(syn_id)
        self.assertEqual(state["status"], "synthesizing")
        self.assertEqual(state["attempt_index"], 1)
        self.assertTrue(state["reflection_coverage"]["complete"])
        self.assertIn("reflections stand", state["revision_context"])
        self.assertIn("Consider revising", state["revision_context"])
        # The graph + reflection-doc + change-spec associations stand too: resubmit directly.
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        self.assertEqual(out["status"], "reflection_review")

    def test_redo_reflection_bumps_attempt_and_resets_coverage(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="reflecting",
            synopsis="The five lenses produced near-duplicate reflections, so the fan-out must re-run.",
            notes="the lenses overlapped; reflections are near-duplicates",
        )
        state = self._state(syn_id)
        self.assertEqual(state["status"], "reflecting")
        self.assertEqual(state["attempt_index"], 2)
        # Old reflections were attempt-1 associations: all lenses owe fresh ones.
        self.assertEqual(set(state["reflection_coverage"]["missing"]), set(ALL_LENS_IDS))
        self.assertIn("re-launch the reflection fan-out", state["revision_context"])
        with self.assertRaises(WorkflowError):
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="submit_reflections",
            )
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        out = self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflections",
        )
        self.assertEqual(out["status"], "synthesizing")

    def test_producer_session_cannot_review_its_own_synthesis(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_reviewer",
            producer_session_id="orchestrator",
        )
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.start",
                review_request_id=req["review_request_id"],
                reviewer_capability=req["reviewer_capability"],
                caller_session_id="orchestrator",
            )

    def test_review_capability_pins_the_synthesis_snapshot(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_reviewer",
        )
        # The producer edits + re-registers the graph after the capability was
        # issued: the association version moves, so the snapshot no longer
        # matches and the reviewer may not start.
        (self.repo / "project/logic_graph.json").write_text(
            VALID_PROJECT_GRAPH.replace("LR schedule", "Warmup schedule")
        )
        res = self.call(
            "resource.register_file", project_id=self.project_id, path="project/logic_graph.json"
        )
        self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=res["id"],
            target_type="reflection",
            target_id=syn_id,
            role="project_graph",
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.call(
                "review.start",
                review_request_id=req["review_request_id"],
                reviewer_capability=req["reviewer_capability"],
                caller_session_id="reviewer",
            )
        self.assertIn("target changed", str(ctx.exception))

    # ---- terminal + discovery ----

    def test_published_is_terminal(self) -> None:
        syn_id = self._drive_to_published()
        with self.assertRaises(WorkflowError):
            self.call(
                "reflection.transition",
                project_id=self.project_id,
                reflection_id=syn_id,
                transition="abandon",
            )
        # A published wave frees the slot for the next one.
        self._create_wave(title="Wave 2")

    def test_get_state_surfaces_allowed_transitions(self) -> None:
        syn_id = self._create_wave()
        state = self._state(syn_id)
        trans = {t["transition"]: t for t in state["allowed_transitions"]}
        self.assertIn("submit_reflections", trans)
        self.assertEqual(trans["submit_reflections"]["leads_to"], "synthesizing")
        self.assertIn("requires", trans["submit_reflections"])
        self.assertIn("abandon", trans)


class ReflectionSignalTest(unittest.TestCase):
    """The drift signal behind project reflection nudges and create blocks."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        # action=current is proxy-served; on the brain the orientation block
        # (with at_a_glance) comes from the ControlApp current_project method.
        current = self.app.current_project()
        self.project_id = current["project"]["id"]
        self.call("project.update", project_id=self.project_id, name="Signal Test")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _finish_experiment(self, *, intent: str) -> str:
        # abandon is the cheapest terminal state; the signal counts all
        # terminal experiments, not just complete ones (dead ends are corpus).
        name = "".join(ch if ch.isalnum() else "-" for ch in intent)[:48]
        exp = self.call("experiment.create", name=name, project_id=self.project_id, intent=intent)
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp["id"],
            transition="abandon",
        )
        return exp["id"]

    def _signal(self) -> dict:
        return self.app.reflection_waves.reflection_signal(project_id=self.project_id)

    def _publish_wave(self) -> str:
        syn = self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        syn_id = syn["id"]
        for lens_id in ALL_LENS_IDS:
            path = self.repo / f"syntheses/{syn_id}/reflections/{lens_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{lens_id} findings\n")
            res = self.call(
                "resource.register_file",
                project_id=self.project_id,
                path=str(path.relative_to(self.repo)),
            )
            self.call(
                "resource.associate",
                project_id=self.project_id,
                resource_id=res["id"],
                target_type="reflection",
                target_id=syn_id,
                role="reflection_lens_doc",
            )
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflections",
        )
        for path, role, body in (
            ("project/logic_graph.json", "project_graph", VALID_PROJECT_GRAPH),
            ("project/reflection.md", "reflection_doc", VALID_REFLECTION_DOC),
            ("project/change_spec.json", "change_spec", VALID_CHANGE_SPEC),
        ):
            full = self.repo / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body)
            res = self.call("resource.register_file", project_id=self.project_id, path=path)
            self.call(
                "resource.associate",
                project_id=self.project_id,
                resource_id=res["id"],
                target_type="reflection",
                target_id=syn_id,
                role=role,
            )
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="submit_reflection_artifacts",
        )
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_reviewer",
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="reviewer",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The reflection wave honestly represents the project's logic state.",
        )
        self.call(
            "reflection.transition",
            project_id=self.project_id,
            reflection_id=syn_id,
            transition="publish",
        )
        return syn_id

    def test_corpus_delta_names_new_signal_and_previous_lens_reflections(self) -> None:
        first = self._finish_experiment(intent="first signal")
        wave1_id = self._publish_wave()
        corpus1 = self.call(
            "reflection.get", project_id=self.project_id, reflection_id=wave1_id
        )["corpus"]
        # The first wave's new signal is everything terminal so far.
        self.assertEqual(
            [exp["id"] for exp in corpus1["new_terminal_experiments"]], [first]
        )
        self.assertIsNone(corpus1["previous_published_reflection_id"])
        self.assertEqual(corpus1["previous_lens_reflections"], {})

        second = self._finish_experiment(intent="second signal")
        corpus2 = self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Wave 2",
            lenses=full_roster(),
        )["corpus"]
        # The second wave's delta excludes what wave 1 already covered and
        # points each lens at its own previous reflection.
        self.assertEqual(
            [exp["id"] for exp in corpus2["new_terminal_experiments"]], [second]
        )
        self.assertEqual(corpus2["previous_published_reflection_id"], wave1_id)
        self.assertEqual(
            corpus2["previous_lens_reflections"],
            {
                lens_id: f"syntheses/{wave1_id}/reflections/{lens_id}.md"
                for lens_id in ALL_LENS_IDS
            },
        )

    def test_quiet_before_threshold_then_first_reflection_nudge(self) -> None:
        for i in range(REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD - 1):
            self._finish_experiment(intent=f"quiet-{i}")
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertEqual(signal["hint"], "")

        self._finish_experiment(intent="nudge")
        signal = self._signal()
        self.assertTrue(signal["stale"])
        self.assertFalse(signal["experiment_create_blocked"])
        self.assertIn("Consider running the project's first reflection", signal["hint"])

        for i in range(
            REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
            REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
        ):
            self._finish_experiment(intent=f"block-{i}")
        signal = self._signal()
        self.assertTrue(signal["experiment_create_blocked"])
        self.assertIn("required before creating another experiment", signal["hint"])

    def test_publish_resets_the_signal_and_coverage(self) -> None:
        for i in range(REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD):
            self._finish_experiment(intent=f"exp {i}")
        self._publish_wave()
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertFalse(signal["experiment_create_blocked"])
        self.assertEqual(
            signal["covered_terminal_experiments"],
            REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
        )
        self.assertEqual(signal["new_terminal_since_publish"], 0)
        # Two more finished experiments: visible but below the nudge threshold.
        self._finish_experiment(intent="post-one")
        self._finish_experiment(intent="post-two")
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertEqual(signal["new_terminal_since_publish"], 2)
        # The third crosses the advisory nudge threshold, but not the hard
        # create block.
        self._finish_experiment(intent="post-three")
        signal = self._signal()
        self.assertTrue(signal["stale"])
        self.assertFalse(signal["experiment_create_blocked"])
        self.assertTrue(signal["hint"].startswith("Consider running a project reflection"))
        self.assertIn("covers 5 of 8", signal["hint"])

        self._finish_experiment(intent="post-four")
        self._finish_experiment(intent="post-five")
        signal = self._signal()
        self.assertTrue(signal["experiment_create_blocked"])

    def test_project_current_includes_reflection_at_a_glance(self) -> None:
        self._finish_experiment(intent="before reflection")
        syn_id = self._publish_wave()
        after_id = self._finish_experiment(intent="after reflection")

        current = self.app.current_project()
        glance = current["at_a_glance"]
        recent = glance["recent"]
        project_reflection = glance["project_reflection"]
        since_reflection = glance["since_reflection"]

        self.assertIn("Latest reflection covers 1/2 finished experiments", glance["summary"])
        self.assertIn(after_id, {item["id"] for item in recent["experiments"]})
        self.assertTrue(
            {"id", "name", "status"}.issubset(recent["experiments"][0])
        )
        self.assertTrue(recent["claims"])
        self.assertTrue(
            {"id", "status", "confidence", "statement"}.issubset(recent["claims"][0])
        )
        self.assertEqual(project_reflection["reflection_id"], syn_id)
        self.assertTrue(project_reflection["reflection_doc_resource_id"])
        self.assertTrue(project_reflection["project_graph_resource_id"])
        self.assertIn(after_id, since_reflection["finished_experiment_ids"])
        self.assertGreaterEqual(len(since_reflection["active_experiment_ids"]), 2)
        self.assertIsNone(glance["open_reflection_id"])

    def test_contradicted_claim_flip_triggers_the_nudge(self) -> None:
        claim = self.call(
            "claim.create", project_id=self.project_id, statement="X helps", confidence="medium"
        )
        for i in range(3):
            self._finish_experiment(intent=f"exp {i}")
        self._publish_wave()
        self.assertFalse(self._signal()["stale"])
        self.call(
            "claim.update",
            project_id=self.project_id,
            claim_id=claim["id"],
            status="contradicted",
        )
        signal = self._signal()
        self.assertTrue(signal["stale"])
        self.assertTrue(signal["contradicted_flip"])
        self.assertFalse(signal["experiment_create_blocked"])
        self.assertIn("contradicted", signal["hint"])

    def test_open_wave_suppresses_the_nudge(self) -> None:
        for i in range(3):
            self._finish_experiment(intent=f"exp {i}")
        self.assertTrue(self._signal()["stale"])
        self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertTrue(signal["open_reflection_id"])

    def test_open_wave_does_not_bypass_hard_experiment_create_block(self) -> None:
        for i in range(REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD):
            self._finish_experiment(intent=f"blocked-{i}")
        self.assertTrue(self._signal()["experiment_create_blocked"])
        self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertTrue(signal["open_reflection_id"])
        self.assertTrue(signal["experiment_create_blocked"])

        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.create",
                project_id=self.project_id,
                name="blocked-create",
                intent="Should wait for reflection publish.",
            )
        self.assertIn("reflection wave", str(ctx.exception))


class StatusAndNextReflectionTest(unittest.TestCase):
    """workflow.status_and_next carries the project_reflection block: wave
    guidance while one is open, the soft hint when stale, nothing otherwise.

    Plus the idle escalation: a project-level call (no explicit
    experiment_id) on an idle project with at least one newly-finished
    experiment makes reflection the suggested next action — positional
    emphasis only, never a gate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="WSN Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _finish_experiment(self, name: str) -> str:
        exp = self.call(
            "experiment.create", name=name, project_id=self.project_id, intent=name
        )
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp["id"],
            transition="abandon",
        )
        return exp["id"]

    def test_absent_when_nothing_to_say(self) -> None:
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        self.assertNotIn("project_reflection", out)
        self.assertEqual(out["workflow"]["current_gate"], "project_setup")

    def test_stale_project_surfaces_the_soft_hint(self) -> None:
        for i in range(3):
            self._finish_experiment(f"stale-{i}")
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        reflection = out["project_reflection"]
        self.assertIsNone(reflection["reflection"])
        self.assertIn("Consider", reflection["hint"])
        self.assertFalse(reflection["experiment_create_blocked"])
        # Stale AND idle: the workflow block recommends the reflection too.
        self.assertTrue(reflection["recommended"])
        self.assertEqual(out["workflow"]["current_gate"], "reflection_suggested")

    def test_idle_project_with_one_new_result_recommends_reflection(self) -> None:
        # One finished experiment is below the staleness threshold, but the
        # project is idle and there is something new to reflect on: the
        # project-level next_action suggests the reflection.
        self._finish_experiment("only-one")
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        reflection = out["project_reflection"]
        self.assertFalse(reflection["signal"]["stale"])
        self.assertTrue(reflection["recommended"])
        self.assertIn("No experiments are active", reflection["hint"])
        workflow = out["workflow"]
        self.assertEqual(workflow["current_gate"], "reflection_suggested")
        self.assertIn("reflection.create", workflow["allowed_actions"])
        # Never a gate: creating the next claim/experiment stays allowed.
        self.assertIn("claim.create", workflow["allowed_actions"])
        self.assertIn("experiment.create", workflow["allowed_actions"])
        self.assertEqual(workflow["blocked_actions"], [])

    def test_blocking_threshold_requires_reflection_before_new_experiment(self) -> None:
        for i in range(REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD):
            self._finish_experiment(f"blocked-{i}")
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        reflection = out["project_reflection"]
        self.assertTrue(reflection["experiment_create_blocked"])
        workflow = out["workflow"]
        self.assertEqual(workflow["current_gate"], "reflection_required")
        self.assertIn("reflection.create", workflow["allowed_actions"])
        self.assertIn("claim.create", workflow["allowed_actions"])
        self.assertNotIn("experiment.create", workflow["allowed_actions"])
        self.assertEqual(workflow["blocked_actions"][0]["action"], "experiment.create")

        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.create",
                project_id=self.project_id,
                name="blocked-create",
                intent="Should require reflection first.",
            )
        self.assertIn("project reflection is required", str(ctx.exception))

    def test_active_experiment_suppresses_the_recommendation(self) -> None:
        # New material exists, but another experiment is in flight: no
        # takeover (the agent should get back to in-flight work), and below
        # the staleness threshold there is no side block either.
        self._finish_experiment("done-1")
        self.call(
            "experiment.create", name="active", project_id=self.project_id, intent="next"
        )
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        self.assertNotEqual(out["workflow"]["current_gate"], "reflection_suggested")
        self.assertNotIn("project_reflection", out)

    def test_stale_with_active_experiment_keeps_soft_hint_only(self) -> None:
        for i in range(3):
            self._finish_experiment(f"done-{i}")
        self.call(
            "experiment.create", name="active", project_id=self.project_id, intent="next"
        )
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        self.assertNotEqual(out["workflow"]["current_gate"], "reflection_suggested")
        reflection = out["project_reflection"]
        self.assertNotIn("recommended", reflection)
        self.assertIn("Consider", reflection["hint"])

    def test_explicit_experiment_scope_is_never_taken_over(self) -> None:
        exp_id = self._finish_experiment("done-1")
        out = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=exp_id,
        )
        # The caller asked about this experiment: its workflow block stands;
        # the side block still carries the recommendation.
        self.assertEqual(out["workflow"]["current_gate"], "terminal")
        self.assertTrue(out["project_reflection"]["recommended"])

    def test_open_wave_carries_gate_guidance(self) -> None:
        self.call(
            "reflection.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        reflection = out["project_reflection"]
        self.assertEqual(reflection["reflection"]["status"], "reflecting")
        workflow = reflection["workflow"]
        self.assertEqual(workflow["current_gate"], "reflection_roster_incomplete")
        self.assertEqual(len(workflow["missing_evidence"]), 5)
        self.assertEqual(len(reflection["reflection"]["roster"]), 5)
        # Idle project-level call: the wave's guidance also takes the
        # top-level workflow slot, so the orientation call drives the wave.
        self.assertEqual(out["workflow"]["current_gate"], "reflection_roster_incomplete")


class StatusAndNextLiveSiblingsTest(unittest.TestCase):
    """The auto-resolved orientation never answers 'none' over live work:
    when the newest-created experiment is terminal while siblings are still
    live, the workflow block lists the live experiments to re-orient onto
    (or create the next one)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Live Siblings")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _finish_experiment(self, name: str) -> str:
        exp = self.call(
            "experiment.create", name=name, project_id=self.project_id, intent=name
        )
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp["id"],
            transition="abandon",
        )
        return exp["id"]

    def _backdate(self, experiment_id: str, created_at: str) -> None:
        # created_at is second-precision, so back-to-back creations tie and
        # the id tiebreak is random; pin creation order explicitly.
        with self.app._store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET created_at = ? WHERE id = ?",
                (created_at, experiment_id),
            )

    def test_newest_terminal_lists_live_siblings(self) -> None:
        live = self.call(
            "experiment.create",
            name="live-a",
            project_id=self.project_id,
            intent="keep tending this",
        )
        self._backdate(live["id"], "2026-01-01T00:00:00Z")
        self._finish_experiment("done-b")
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        # The scope still resolves to the finished newest experiment...
        self.assertEqual(out["experiment"]["status"], "abandoned")
        # ...but the workflow block re-orients instead of answering 'none'.
        workflow = out["workflow"]
        self.assertEqual(workflow["current_gate"], "live_experiments")
        self.assertIn("workflow.status_and_next", workflow["allowed_actions"])
        self.assertIn("experiment.create", workflow["allowed_actions"])
        entries = workflow["live_experiments"]
        self.assertEqual([item["id"] for item in entries], [live["id"]])
        self.assertEqual(entries[0]["name"], "live-a")
        self.assertEqual(entries[0]["status"], "planned")
        self.assertEqual(entries[0]["intent"], "keep tending this")

    def test_explicit_scope_keeps_the_terminal_answer(self) -> None:
        live = self.call(
            "experiment.create",
            name="live-a",
            project_id=self.project_id,
            intent="still running",
        )
        self._backdate(live["id"], "2026-01-01T00:00:00Z")
        done_id = self._finish_experiment("done-b")
        out = self.call(
            "workflow.status_and_next",
            project_id=self.project_id,
            experiment_id=done_id,
        )
        self.assertEqual(out["workflow"]["current_gate"], "terminal")
        self.assertNotIn("live_experiments", out["workflow"])

    def test_newest_live_resolves_normally(self) -> None:
        done_id = self._finish_experiment("done-a")
        self._backdate(done_id, "2026-01-01T00:00:00Z")
        self.call(
            "experiment.create",
            name="live-b",
            project_id=self.project_id,
            intent="the next one",
        )
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        self.assertEqual(out["workflow"]["current_gate"], "plan_required")
        self.assertNotIn("live_experiments", out["workflow"])

    def test_hard_reflection_block_carries_into_the_takeover(self) -> None:
        live = self.call(
            "experiment.create",
            name="live-first",
            project_id=self.project_id,
            intent="oldest, still live",
        )
        self._backdate(live["id"], "2026-01-01T00:00:00Z")
        for i in range(REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD):
            self._finish_experiment(f"done-{i}")
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        workflow = out["workflow"]
        self.assertEqual(workflow["current_gate"], "live_experiments")
        self.assertNotIn("experiment.create", workflow["allowed_actions"])
        self.assertEqual(
            workflow["blocked_actions"][0]["action"], "experiment.create"
        )
        self.assertTrue(workflow["blocked_actions"][0]["reason"])
        self.assertEqual(len(workflow["live_experiments"]), 1)


if __name__ == "__main__":
    unittest.main()
