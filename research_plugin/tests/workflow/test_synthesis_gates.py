from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.utils import PermissionDeniedError, ValidationError, WorkflowError

# A project logic graph that satisfies the envelope lint (valid JSON, ≤16
# nodes, DAG) — the same lint experiments use, one level up.
VALID_PROJECT_GRAPH = (
    '{"version": 1, "title": "Project logic", "nodes": ['
    '{"id": "lesson", "kind": "lesson", "label": "LR schedule dominates"},'
    '{"id": "open", "kind": "open_question", "label": "Does it hold at scale?"}],'
    ' "edges": [{"from": "lesson", "to": "open", "label": "raises"}]}\n'
)

VALID_PROPOSALS = (
    "## Proposal 1 — scale check\n"
    "Hypothesis: the LR-schedule win survives a 10x larger model.\n"
    "builds_on: exp_a\n"
    "Moves claim: claim_b\n"
)

# A full 5-lens roster: the three core lenses plus two wave-authored ones,
# each with a charter and a stated distinctness reason.
def full_roster() -> list[dict[str, str]]:
    return [
        {"id": "outcomes"},
        {"id": "dead_ends"},
        {"id": "coverage"},
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


ALL_LENS_IDS = ("outcomes", "dead_ends", "coverage", "rigor", "cost")


class SynthesisGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="Synthesis Gate Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _create_wave(self, *, title: str = "Wave") -> str:
        return self.call(
            "synthesis.create", project_id=self.project_id, title=title, lenses=full_roster()
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
            target_type="synthesis",
            target_id=syn_id,
            role=role,
        )
        return res["id"]

    def _submit_reflection(self, *, syn_id: str, lens_id: str) -> None:
        self._associate_file(
            syn_id=syn_id,
            path=f"syntheses/{syn_id}/reflections/{lens_id}.md",
            role="reflection",
            body=f"# {lens_id}\nFindings through the {lens_id} lens.\n",
        )

    def _drive_to_synthesizing(self) -> str:
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_reflections",
        )
        return syn_id

    def _drive_to_synthesis_review(self) -> str:
        syn_id = self._drive_to_synthesizing()
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/proposals.md", role="proposals", body=VALID_PROPOSALS
        )
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_synthesis",
        )
        return syn_id

    def _open_review_session(self, *, syn_id: str, caller: str = "synthesis-reviewer") -> str:
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="synthesis",
            target_id=syn_id,
            role="synthesis_reviewer",
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
        self.call("review.submit", review_session_id=session_id, verdict="pass")
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="publish",
        )
        return syn_id

    def _state(self, syn_id: str) -> dict:
        return self.call("synthesis.get", project_id=self.project_id, synthesis_id=syn_id)

    # ---- roster envelope ----

    def test_roster_must_be_exactly_five_lenses(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=[])
        self.assertIn("exactly 5 lenses", str(ctx.exception))
        with self.assertRaises(ValidationError):
            self.call(
                "synthesis.create", project_id=self.project_id, lenses=full_roster()[:4]
            )

    def test_roster_requires_all_core_lenses(self) -> None:
        roster = full_roster()
        roster[0] = {
            "id": "vibes",
            "charter": "General vibes.",
            "why_distinct": "It is vibes.",
        }
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=roster)
        self.assertIn("missing core lens(es): outcomes", str(ctx.exception))

    def test_authored_lenses_require_charter_and_why_distinct(self) -> None:
        roster = full_roster()
        roster[3] = {"id": "rigor", "charter": "Method soundness."}  # no why_distinct
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=roster)
        self.assertIn("why_distinct", str(ctx.exception))
        roster[3] = {"id": "rigor", "why_distinct": "Different."}  # no charter
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=roster)
        self.assertIn("charter", str(ctx.exception))

    def test_roster_rejects_duplicate_and_malformed_ids(self) -> None:
        roster = full_roster()
        roster[4] = dict(roster[3])
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=roster)
        self.assertIn("duplicate lens id", str(ctx.exception))
        roster = full_roster()
        roster[4]["id"] = "Not A Slug!"
        with self.assertRaises(ValidationError) as ctx:
            self.call("synthesis.create", project_id=self.project_id, lenses=roster)
        self.assertIn("invalid lens id", str(ctx.exception))

    def test_core_lenses_default_their_charters(self) -> None:
        syn = self._state(self._create_wave())
        by_id = {lens["id"]: lens for lens in syn["roster"]}
        self.assertTrue(by_id["outcomes"]["core"])
        self.assertIn("verified knowledge state", by_id["outcomes"]["charter"])
        self.assertIn("negative-knowledge ledger", by_id["dead_ends"]["charter"])
        self.assertFalse(by_id["rigor"]["core"])

    def test_only_one_wave_may_be_open(self) -> None:
        self._create_wave()
        with self.assertRaises(WorkflowError) as ctx:
            self._create_wave(title="Second")
        self.assertIn("already open", str(ctx.exception))
        # A terminal wave (here: abandoned) frees the slot.
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=self._state_id_of_open_wave(),
            transition="abandon",
        )
        self._create_wave(title="Third")

    def _state_id_of_open_wave(self) -> str:
        syntheses = self.call("synthesis.list", project_id=self.project_id)["syntheses"]
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
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_reflections",
            )
        # Four of five: the error names exactly the missing lens.
        for lens_id in ALL_LENS_IDS[:-1]:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("cost", str(ctx.exception))
        self.assertNotIn("outcomes,", str(ctx.exception))
        self._submit_reflection(syn_id=syn_id, lens_id="cost")
        out = self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_reflections",
        )
        self.assertEqual(out["status"], "synthesizing")

    def test_reflection_must_be_named_after_its_lens(self) -> None:
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS[:-1]:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        # role 'reflection' but the filename stem matches no lens ⇒ not coverage.
        self._associate_file(
            syn_id=syn_id,
            path=f"syntheses/{syn_id}/reflections/notes.md",
            role="reflection",
            body="loose notes\n",
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("cost", str(ctx.exception))

    def test_empty_reflection_blocks_submit(self) -> None:
        syn_id = self._create_wave()
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        (self.repo / f"syntheses/{syn_id}/reflections/rigor.md").write_text("   \n")
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_reflections",
            )
        self.assertIn("empty", str(ctx.exception))

    def test_get_state_reports_reflection_coverage(self) -> None:
        syn_id = self._create_wave()
        self._submit_reflection(syn_id=syn_id, lens_id="outcomes")
        coverage = self._state(syn_id)["reflection_coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(
            set(coverage["missing"]), {"dead_ends", "coverage", "rigor", "cost"}
        )

    # ---- synthesis artifacts gate ----

    def test_submit_synthesis_requires_graph_then_proposals(self) -> None:
        syn_id = self._drive_to_synthesizing()
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_synthesis",
            )
        self.assertIn("project logic graph", str(ctx.exception))
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="graph", body=VALID_PROJECT_GRAPH
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_synthesis",
            )
        self.assertIn("proposals", str(ctx.exception))
        self._associate_file(
            syn_id=syn_id, path="project/proposals.md", role="proposals", body=VALID_PROPOSALS
        )
        out = self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_synthesis",
        )
        self.assertEqual(out["status"], "synthesis_review")

    def test_project_graph_over_budget_is_rejected_plainly(self) -> None:
        syn_id = self._drive_to_synthesizing()
        nodes = [{"id": f"n{i}", "label": f"Node {i}"} for i in range(17)]
        self._associate_file(
            syn_id=syn_id,
            path="project/logic_graph.json",
            role="graph",
            body=json.dumps({"version": 1, "nodes": nodes}),
        )
        self._associate_file(
            syn_id=syn_id, path="project/proposals.md", role="proposals", body=VALID_PROPOSALS
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_synthesis",
            )
        message = str(ctx.exception)
        self.assertIn("reduce the graph", message)
        self.assertNotIn("collapse", message)
        self.assertNotIn("merge", message)

    def test_empty_proposals_file_is_rejected(self) -> None:
        syn_id = self._drive_to_synthesizing()
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/proposals.md", role="proposals", body="  \n"
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_synthesis",
            )
        self.assertIn("empty", str(ctx.exception))

    # ---- review gate + routing ----

    def test_publish_requires_a_passing_synthesis_review(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        with self.assertRaises(WorkflowError):
            self.call(
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="publish",
            )
        session_id = self._open_review_session(syn_id=syn_id)
        self.call("review.submit", review_session_id=session_id, verdict="pass")
        out = self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="publish",
        )
        self.assertEqual(out["status"], "published")
        self.assertTrue(out["published_at"])
        self.assertTrue(out["published_graph_version_id"])

    def test_review_request_role_must_match_the_synthesis_gate(self) -> None:
        syn_id = self._create_wave()
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "review.request",
                project_id=self.project_id,
                target_type="synthesis",
                target_id=syn_id,
                role="synthesis_reviewer",
            )
        # Only one wave may be open, so drive this same one forward.
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_reflections",
        )
        self._associate_file(
            syn_id=syn_id, path="project/logic_graph.json", role="graph", body=VALID_PROJECT_GRAPH
        )
        self._associate_file(
            syn_id=syn_id, path="project/proposals.md", role="proposals", body=VALID_PROPOSALS
        )
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_synthesis",
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.call(
                "review.request",
                project_id=self.project_id,
                target_type="synthesis",
                target_id=syn_id,
                role="experiment_reviewer",
            )
        self.assertIn("synthesis_reviewer", str(ctx.exception))

    def test_synthesis_rejection_requires_explicit_return_to(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        with self.assertRaises(ValidationError) as ctx:
            self.call(
                "review.submit",
                review_session_id=session_id,
                verdict="needs_changes",
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
            )

    def test_redo_synthesis_keeps_reflections_and_attempt(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="synthesizing",
            notes="a dead end is retold as a near-win",
        )
        state = self._state(syn_id)
        self.assertEqual(state["status"], "synthesizing")
        self.assertEqual(state["attempt_index"], 1)
        self.assertTrue(state["reflection_coverage"]["complete"])
        self.assertIn("reflections stand", state["revision_context"])
        self.assertIn("Consider revising", state["revision_context"])
        # The graph + proposals associations stand too: resubmit directly.
        out = self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_synthesis",
        )
        self.assertEqual(out["status"], "synthesis_review")

    def test_redo_reflection_bumps_attempt_and_resets_coverage(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        session_id = self._open_review_session(syn_id=syn_id)
        self.call(
            "review.submit",
            review_session_id=session_id,
            verdict="needs_changes",
            return_to="reflecting",
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
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
                transition="submit_reflections",
            )
        for lens_id in ALL_LENS_IDS:
            self._submit_reflection(syn_id=syn_id, lens_id=lens_id)
        out = self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_reflections",
        )
        self.assertEqual(out["status"], "synthesizing")

    def test_producer_session_cannot_review_its_own_synthesis(self) -> None:
        syn_id = self._drive_to_synthesis_review()
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="synthesis",
            target_id=syn_id,
            role="synthesis_reviewer",
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
            target_type="synthesis",
            target_id=syn_id,
            role="synthesis_reviewer",
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
            target_type="synthesis",
            target_id=syn_id,
            role="graph",
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
                "synthesis.transition",
                project_id=self.project_id,
                synthesis_id=syn_id,
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
    """The staleness signal behind the soft 'Consider running a project
    reflection' nudge. Always advisory — these tests pin the threshold and
    the soft phrasing, not any enforcement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="Signal Test")["id"]

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
        return self.app.syntheses.reflection_signal(project_id=self.project_id)

    def _publish_wave(self) -> str:
        syn = self.call(
            "synthesis.create",
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
                target_type="synthesis",
                target_id=syn_id,
                role="reflection",
            )
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_reflections",
        )
        for path, role, body in (
            ("project/logic_graph.json", "graph", VALID_PROJECT_GRAPH),
            ("project/proposals.md", "proposals", VALID_PROPOSALS),
        ):
            full = self.repo / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body)
            res = self.call("resource.register_file", project_id=self.project_id, path=path)
            self.call(
                "resource.associate",
                project_id=self.project_id,
                resource_id=res["id"],
                target_type="synthesis",
                target_id=syn_id,
                role=role,
            )
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="submit_synthesis",
        )
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="synthesis",
            target_id=syn_id,
            role="synthesis_reviewer",
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="reviewer",
        )
        self.call("review.submit", review_session_id=session["review_session_id"], verdict="pass")
        self.call(
            "synthesis.transition",
            project_id=self.project_id,
            synthesis_id=syn_id,
            transition="publish",
        )
        return syn_id

    def test_quiet_before_threshold_then_first_reflection_nudge(self) -> None:
        self._finish_experiment(intent="one")
        self._finish_experiment(intent="two")
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertEqual(signal["hint"], "")
        self._finish_experiment(intent="three")
        signal = self._signal()
        self.assertTrue(signal["stale"])
        self.assertIn("Consider running the project's first reflection", signal["hint"])

    def test_publish_resets_the_signal_and_coverage(self) -> None:
        for i in range(3):
            self._finish_experiment(intent=f"exp {i}")
        self._publish_wave()
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertEqual(signal["covered_terminal_experiments"], 3)
        self.assertEqual(signal["new_terminal_since_publish"], 0)
        # Two more finished experiments: visible but below threshold.
        self._finish_experiment(intent="four")
        self._finish_experiment(intent="five")
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertEqual(signal["new_terminal_since_publish"], 2)
        # The third crosses it; the hint stays soft ('Consider …').
        self._finish_experiment(intent="six")
        signal = self._signal()
        self.assertTrue(signal["stale"])
        self.assertTrue(signal["hint"].startswith("Consider running a project reflection"))
        self.assertIn("covers 3 of 6", signal["hint"])

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
        self.assertIn("contradicted", signal["hint"])

    def test_open_wave_suppresses_the_nudge(self) -> None:
        for i in range(3):
            self._finish_experiment(intent=f"exp {i}")
        self.assertTrue(self._signal()["stale"])
        self.call(
            "synthesis.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        signal = self._signal()
        self.assertFalse(signal["stale"])
        self.assertTrue(signal["open_synthesis_id"])


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
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="WSN Test")["id"]

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
        self.assertIsNone(reflection["synthesis"])
        self.assertIn("Consider", reflection["hint"])
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
        self.assertIn("synthesis.create", workflow["allowed_actions"])
        # Never a gate: creating the next claim/experiment stays allowed.
        self.assertIn("claim.create", workflow["allowed_actions"])
        self.assertIn("experiment.create", workflow["allowed_actions"])
        self.assertEqual(workflow["blocked_actions"], [])

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
            "synthesis.create",
            project_id=self.project_id,
            title="Wave",
            lenses=full_roster(),
        )
        out = self.call("workflow.status_and_next", project_id=self.project_id)
        reflection = out["project_reflection"]
        self.assertEqual(reflection["synthesis"]["status"], "reflecting")
        workflow = reflection["workflow"]
        self.assertEqual(workflow["current_gate"], "reflection_roster_incomplete")
        self.assertEqual(len(workflow["missing_evidence"]), 5)
        self.assertEqual(len(reflection["synthesis"]["roster"]), 5)
        # Idle project-level call: the wave's guidance also takes the
        # top-level workflow slot, so the orientation call drives the wave.
        self.assertEqual(out["workflow"]["current_gate"], "reflection_roster_incomplete")


if __name__ == "__main__":
    unittest.main()
