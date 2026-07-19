from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from merv.brain.dataplane.resource_validation import validate_local_resource_artifact


VALID_PLAN = (
    "## Summary\n"
    "A small plan.\n\n"
    "## Objective & hypothesis\n"
    "Try the thing.\n\n"
    "## Evaluation\n"
    "Measure the result.\n"
)

VALID_REPORT = (
    "## Summary\n"
    "Ran the experiment.\n\n"
    "## Results\n\n"
    "| Metric | Target | Achieved |\n"
    "|--------|--------|----------|\n"
    "| accuracy | 0.60 | 0.72 |\n\n"
    "## Deviations from plan\n"
    "None.\n\n"
    "## Conclusion\n"
    "It passed.\n"
)


class ResourceArtifactValidationTest(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "plan.md").write_text(VALID_PLAN, encoding="utf-8")

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="plan.md",
                role="plan",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["gated"])

    def test_plan_reports_missing_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "plan.md").write_text("## Summary\nOnly one section.\n", encoding="utf-8")

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="plan.md",
                role="plan",
            )

        self.assertFalse(result["ok"])
        self.assertIn("Objective & hypothesis", result["problems"][0])
        self.assertIn("Evaluation", result["problems"][0])

    def test_plan_reports_missing_figure_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "plan.md").write_text(
                VALID_PLAN + "\n![arch](figures/diagram.png)\n", encoding="utf-8"
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="plan.md",
                role="plan",
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "figure 'figures/diagram.png' has no submitted content: file does not exist",
            result["problems"],
        )

    def test_report_reports_missing_figure_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "report.md").write_text(
                VALID_REPORT + "\n![loss](figures/loss.png)\n",
                encoding="utf-8",
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="report.md",
                role="report",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("figures/loss.png" in problem for problem in result["problems"])
        )

    def test_graph_reports_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "graph.json").write_text(
                '{"version": 1, "nodes": ['
                '{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], '
                '"edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}',
                encoding="utf-8",
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="graph.json",
                role="graph",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(any("cycle" in problem for problem in result["problems"]))

    def test_missing_file_is_reported_as_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = validate_local_resource_artifact(
                repo_root=Path(tmp),
                path="missing.tsv",
                role="result",
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["gated"])
        self.assertIn("does not exist", result["problems"][0])

    def test_reflection_doc_runs_the_gate_lint(self) -> None:
        # Same lint the reflection transition runs: a doc without the
        # required sections must not preflight green.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "reflection.md").write_text(
                "## Summary\nOnly a summary.\n", encoding="utf-8"
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="reflection.md",
                role="reflection_doc",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("Critical reading" in problem for problem in result["problems"])
        )

    def test_reflection_lens_doc_rejects_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "lens.md").write_text("   \n", encoding="utf-8")

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="lens.md",
                role="reflection_lens_doc",
            )

        self.assertFalse(result["ok"])
        self.assertIn("reflection lens document is empty", result["problems"])

    def test_change_spec_runs_the_structural_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "spec.json").write_text(
                '{"version": 99, "claim_changes": "nope"}', encoding="utf-8"
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="spec.json",
                role="change_spec",
            )

        self.assertFalse(result["ok"])
        self.assertIn("version must be 1", result["problems"])
        self.assertIn("claim_changes must be a list", result["problems"])
        self.assertIn("decision must be an object", result["problems"])

    def test_one_escaping_figure_does_not_fail_its_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "figs").mkdir()
            (repo / "figs" / "ok.png").write_bytes(b"png bytes")
            (repo / "report.md").write_text(
                VALID_REPORT + "\n![bad](../outside.png)\n![good](figs/ok.png)\n",
                encoding="utf-8",
            )

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="report.md",
                role="report",
            )

        escapes = [p for p in result["problems"] if "escapes the repo" in p]
        self.assertEqual(len(escapes), 1)
        self.assertFalse(any("ok.png" in problem for problem in result["problems"]))

    def test_oversized_gated_file_skips_content_lint(self) -> None:
        # Parity with association: the gate refuses on size before reading
        # content, so the preflight reports the cap and nothing else.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "plan.md").write_text("x" * 20_000, encoding="utf-8")

            result = validate_local_resource_artifact(
                repo_root=repo,
                path="plan.md",
                role="plan",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("maximum for a role-'plan' artifact", result["problems"][0])


if __name__ == "__main__":
    unittest.main()
