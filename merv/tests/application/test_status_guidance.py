from __future__ import annotations

import ast
import unittest

from merv.brain.application.status_guidance import (
    StatusGuidancePolicy,
)
from merv.brain.application.guidance_catalog import (
    EXPERIMENT_READY,
    EXPERIMENT_REQUIREMENTS,
    REFLECTION_READY,
    REFLECTION_REQUIREMENTS,
    REVIEWS,
)
from merv.brain.research_core.domain.reflection_gates import REFLECTION_GATE_TABLE
from merv.brain.research_core.domain.workflow_gates import GATE_TABLE
from merv.brain.research_core.facade import GateEvaluation, RequirementEvaluation
from tests.paths import BACKEND_ROOT


def _requirement(
    role: str,
    status: str,
    blocker: str,
    *,
    problems: tuple[str, ...] = (),
    items: tuple[dict[str, object], ...] = (),
) -> RequirementEvaluation:
    error = problems[0] if problems else f"{role} required"
    return RequirementEvaluation(role, status, blocker, error, problems, items)


def _evaluation(
    *,
    subject: str = "experiment",
    status: str,
    transition: str | None,
    requirements: tuple[RequirementEvaluation, ...] = (),
    review: RequirementEvaluation | None = None,
) -> GateEvaluation:
    return GateEvaluation(
        subject=subject,
        status=status,
        transition=transition,
        leads_to=None,
        terminal=transition is None,
        requirements=requirements,
        review=review,
        legal_transitions=(),
    )


class StatusGuidanceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = StatusGuidancePolicy()

    def test_every_research_gate_fact_has_application_guidance(self) -> None:
        self.assertEqual(
            set(EXPERIMENT_REQUIREMENTS),
            {
                requirement.role
                for forward in GATE_TABLE.values()
                for requirement in forward.requirements
            },
        )
        self.assertEqual(
            set(REFLECTION_REQUIREMENTS),
            {
                requirement.role
                for forward in REFLECTION_GATE_TABLE.values()
                for requirement in forward.requirements
            },
        )
        self.assertEqual(
            set(EXPERIMENT_READY),
            {
                forward.name
                for forward in GATE_TABLE.values()
                if forward.review is None
            },
        )
        self.assertEqual(
            set(REFLECTION_READY),
            {
                forward.name
                for forward in REFLECTION_GATE_TABLE.values()
                if forward.review is None
            },
        )
        self.assertEqual(
            set(REVIEWS),
            {
                forward.review.role
                for table in (GATE_TABLE, REFLECTION_GATE_TABLE)
                for forward in table.values()
                if forward.review is not None
            },
        )

    def test_policy_imports_only_the_public_research_entrypoint(self) -> None:
        path = BACKEND_ROOT / "application" / "status_guidance.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        research_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and "research_core" in node.module
        }
        self.assertEqual(research_imports, {"research_core.facade"})

    def test_missing_requirement_precedes_an_earlier_invalid_one(self) -> None:
        result = self.policy.experiment(
            experiment={"id": "exp_1", "name": "example", "status": "running"},
            sandboxes=[],
            evaluation=_evaluation(
                status="running",
                transition="submit_results",
                requirements=(
                    _requirement("result", "present", "", items=({},)),
                    _requirement(
                        "report",
                        "invalid",
                        "report_invalid",
                        problems=("report is invalid",),
                    ),
                    _requirement(
                        "graph",
                        "missing",
                        "logic_graph_required",
                        items=(
                            {
                                "status": "missing",
                                "missing": "logic graph resource (role 'graph')",
                            },
                        ),
                    ),
                ),
            ),
        )
        self.assertEqual(result["current_gate"], "logic_graph_required")
        self.assertEqual(result["next_action"], "write_and_associate_logic_graph")
        self.assertEqual(
            result["missing_evidence"], ["logic graph resource (role 'graph')"]
        )

    def test_live_sandbox_changes_only_the_execution_gate_name(self) -> None:
        result = self.policy.experiment(
            experiment={"id": "exp_1", "name": "example", "status": "running"},
            sandboxes=[{"status": "running"}],
            evaluation=_evaluation(
                status="running",
                transition="submit_results",
                requirements=(
                    _requirement(
                        "result",
                        "missing",
                        "execution_ready",
                        items=({"status": "missing", "missing": "result resource"},),
                    ),
                ),
            ),
        )
        self.assertEqual(result["current_gate"], "execution_active")
        self.assertEqual(result["next_action"], "run_experiment_and_retain_results")

    def test_review_request_preserves_spawn_guidance_shape(self) -> None:
        result = self.policy.experiment(
            experiment={"id": "exp_1", "status": "design_review"},
            sandboxes=[],
            evaluation=_evaluation(
                status="design_review",
                transition="mark_ready_to_run",
                review=_requirement(
                    "design_reviewer",
                    "requested",
                    "design_review_required",
                    items=(
                        {
                            "request_id": "rr_1",
                            "expires_at": "2026-07-21T18:00:00Z",
                        },
                    ),
                ),
            ),
        )
        self.assertEqual(result["next_action"], "launch_design_reviewer")
        self.assertEqual(
            result["allowed_actions"], ["workflow.status_and_next", "review.request"]
        )
        self.assertEqual(
            result["review_gate"],
            {
                "role": "design_reviewer",
                "skill": "experiment-design-review",
                "target_type": "experiment",
                "target_id": "exp_1",
                "status": "requested",
                "label": "Reviewer pending",
                "read_only": True,
                "request_id": "rr_1",
                "expires_at": "2026-07-21T18:00:00Z",
            },
        )

    def test_reflection_roster_uses_each_factual_missing_lens(self) -> None:
        result = self.policy.project_reflection(
            open_wave={
                "id": "syn_1",
                "status": "reflecting",
                "revision_context": "",
            },
            evaluation=_evaluation(
                subject="reflection wave",
                status="reflecting",
                transition="submit_reflections",
                requirements=(
                    _requirement(
                        "reflection_lens_doc",
                        "missing",
                        "reflection_roster_incomplete",
                        items=(
                            {"status": "missing", "missing": "amplify reflection"},
                            {"status": "missing", "missing": "avoid reflection"},
                        ),
                    ),
                ),
            ),
            signal={"experiment_create_blocked": False},
            idle=True,
        )
        assert result is not None
        self.assertEqual(
            result["workflow"]["missing_evidence"],
            ["amplify reflection", "avoid reflection"],
        )

    def test_idle_reflection_hint_preserves_existing_wording(self) -> None:
        result = self.policy.project_reflection(
            open_wave=None,
            evaluation=None,
            signal={
                "new_terminal_since_publish": 1,
                "contradicted_flip": False,
                "stale": False,
                "experiment_create_blocked": False,
                "last_published_reflection_id": None,
                "claims_changed_since_publish": 0,
            },
            idle=True,
        )
        assert result is not None
        self.assertEqual(result["signal"]["hint"], "")
        self.assertEqual(list(result["signal"])[-1], "hint")
        self.assertEqual(
            result["hint"],
            "No experiments are active and 1 experiment has finished and no "
            "project reflection exists yet — a good moment for a project reflection "
            "(reflection.create, project-reflection skill), or start the next "
            "experiment if the logic state is current.",
        )


if __name__ == "__main__":
    unittest.main()
