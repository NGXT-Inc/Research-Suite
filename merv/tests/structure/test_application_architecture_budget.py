"""Fail-closed size and compatibility-wrapper ratchets for this migration."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "src" / "merv" / "brain"
# Phase 4 physically relocates the 785-line agent policy, adds typed response
# projection, and preserves the existing wire contract. This temporary high
# watermark is intentionally reclaimed by Phase 7's <=40,600 target.
PHASE_3_BRAIN_LOC = 41_265
MAX_BRAIN_LOC = 41_700
BASELINE_SURFACE_ORCHESTRATION_LOC = 1_022
PRE_TRACKING_SURFACE_LOC = 549
MAX_SURFACE_ORCHESTRATION_LOC = 100
PRE_APPLICATION_HTTP_VIEWS_LOC = 763
MAX_HTTP_VIEWS_LOC = 470


class ApplicationArchitectureBudgetTest(unittest.TestCase):
    def test_brain_loc_ceiling(self) -> None:
        current = sum(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in BRAIN.rglob("*.py")
        )
        self.assertLessEqual(current, MAX_BRAIN_LOC)
        self.assertEqual(MAX_BRAIN_LOC - PHASE_3_BRAIN_LOC, 435)

    def test_rewritten_orchestration_hubs_stay_small(self) -> None:
        def lines(relative: str) -> int:
            return len((BRAIN / relative).read_text(encoding="utf-8").splitlines())

        workflow = sum(
            lines(path)
            for path in (
                "application/workflow.py",
                "application/status_guidance.py",
                "research_core/snapshots.py",
            )
        )
        sandbox_handlers = sum(
            lines(f"sandbox/{name}")
            for name in ("commands.py", "queries.py", "handler.py", "maintenance_handler.py")
        )
        self.assertLessEqual(workflow, 1_530)
        self.assertLessEqual(lines("application/status_guidance.py"), 730)
        self.assertLessEqual(lines("application/reflection_guidance.py"), 140)
        self.assertLessEqual(lines("application/experiment_figure.py"), 300)
        self.assertLessEqual(lines("application/experiments/claim_guidance.py"), 60)
        self.assertLessEqual(lines("application/experiments/presentation.py"), 140)
        self.assertLessEqual(lines("application/resource_content.py"), 90)
        self.assertLessEqual(lines("sandbox/sandboxes.py"), 300)
        self.assertLessEqual(sandbox_handlers, 1_050)
        self.assertLessEqual(
            lines("surface/transport/api/app.py")
            + lines("surface/transport/api/gateway.py"),
            500,
        )

    def test_surface_orchestration_shrank_by_at_least_120_lines(self) -> None:
        current = len(
            (BRAIN / "surface/tools/tool_handlers.py")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertLessEqual(current, MAX_SURFACE_ORCHESTRATION_LOC)
        self.assertEqual(PRE_TRACKING_SURFACE_LOC - MAX_SURFACE_ORCHESTRATION_LOC, 449)
        self.assertEqual(
            BASELINE_SURFACE_ORCHESTRATION_LOC - MAX_SURFACE_ORCHESTRATION_LOC,
            922,
        )
        self.assertFalse((BRAIN / "surface/tools/exhibits.py").exists())

    def test_mlflow_compatibility_wrapper_is_import_only(self) -> None:
        path = BRAIN / "mlflow/exhibit.py"
        source = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 15)
        tree = ast.parse(source, filename=str(path))
        self.assertTrue(
            all(
                isinstance(node, ast.ImportFrom)
                or (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
                for node in tree.body
            ),
            "compatibility wrapper may contain only its docstring and imports",
        )

    def test_http_views_stay_delivery_sized(self) -> None:
        current = len(
            (BRAIN / "surface/transport/api/views.py")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertLessEqual(current, MAX_HTTP_VIEWS_LOC)
        self.assertGreaterEqual(
            PRE_APPLICATION_HTTP_VIEWS_LOC - MAX_HTTP_VIEWS_LOC,
            293,
        )

    def test_review_and_reaction_orchestration_stays_out_of_surface(self) -> None:
        handlers = (BRAIN / "surface/tools/tool_handlers.py").read_text(encoding="utf-8")
        transition = (BRAIN / "application/experiments/transition.py").read_text(
            encoding="utf-8"
        )
        tracking = (BRAIN / "application/experiments/tracking.py").read_text(
            encoding="utf-8"
        )
        reactions = (BRAIN / "application/experiments/reactions.py").read_text(
            encoding="utf-8"
        )
        views = (BRAIN / "surface/transport/api/views.py").read_text(
            encoding="utf-8"
        )
        composition = (BRAIN / "surface/control/control_app.py").read_text(
            encoding="utf-8"
        )
        for removed in (
            "def review_status_agent",
            "experiment_review_verdict",
            "build_local_tool_handlers",
        ):
            self.assertNotIn(removed, handlers)
        manifest = (BRAIN / "surface/tools/contracts.py").read_text(encoding="utf-8")
        self.assertIn('handler_identity="review_status.execute"', manifest)
        self.assertIn("for name, tool in TOOL_MANIFEST.items()", handlers)
        for application_decision in (
            "slim_experiment_state",
            "ValidationError",
            "def project_control",
            "def resource_find",
            "def storage_find",
            "def storage_object",
        ):
            self.assertNotIn(application_decision, handlers)
        for use_case in (transition, tracking):
            self.assertNotIn("EventDispatcher()", use_case)
            self.assertNotIn(".register(", use_case)
        self.assertIn(
            '("experiment.mlflow_run_refreshed", "post_response")', reactions
        )
        self.assertIn("self.reaction_registry = EventDispatcher()", composition)
        self.assertEqual(composition.count("dispatcher=self.reaction_registry"), 3)
        self.assertNotIn("self.app.mlflow_tracking", views)
        self.assertNotIn("mlflow_visible_for_status", views)
        self.assertIn("self.app.experiment_detail_query(", views)

    def test_tool_operations_receive_narrow_callable_ports(self) -> None:
        commands = (BRAIN / "application/tool_commands.py").read_text(encoding="utf-8")
        composition = (BRAIN / "surface/control/control_app.py").read_text(
            encoding="utf-8"
        )
        for raw_service in ("projects: Any", "claims: Any", "resources: Any", "storage: Any"):
            self.assertNotIn(raw_service, commands)
        for binding in (
            "project_create=self.projects.create",
            "claims_list=self.claims.list_claims",
            "resource_resolve=self.resources.resolve",
        ):
            self.assertIn(binding, composition)


if __name__ == "__main__":
    unittest.main()
