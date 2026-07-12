from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.dataplane.resource_artifacts import LocalResourceArtifactReader
from backend.artifacts.markdown_images import MARKDOWN_FIGURE_MAX_BYTES
from backend.artifacts.roles import GATED_ROLE_BYTE_CAPS
from backend.utils import NotFoundError, ValidationError


class LocalResourceArtifactReaderTest(unittest.TestCase):
    def test_reads_gated_artifact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "plan.txt").write_bytes(b"## Plan\n")

            observed = LocalResourceArtifactReader(repo_root=repo).read_for_association(
                path="plan.txt",
                role="plan",
            )

        self.assertEqual(observed["content_bytes"], b"## Plan\n")
        self.assertEqual(observed["content_type"], "text/plain")
        self.assertEqual(observed["figures"], [])

    def test_result_role_does_not_read_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observed = LocalResourceArtifactReader(
                repo_root=Path(tmp)
            ).read_for_association(path="missing.txt", role="result")

        self.assertIsNone(observed["content_bytes"])
        self.assertEqual(observed["figures"], [])

    def test_rejects_missing_and_oversized_gated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reader = LocalResourceArtifactReader(repo_root=repo)
            with self.assertRaises(NotFoundError):
                reader.read_for_association(path="missing.md", role="plan")

            (repo / "plan.md").write_bytes(b"x" * (GATED_ROLE_BYTE_CAPS["plan"] + 1))
            with self.assertRaisesRegex(ValidationError, "maximum"):
                reader.read_for_association(path="plan.md", role="plan")

    def test_collects_markdown_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "reports").mkdir()
            (repo / "reports" / "fig.png").write_bytes(b"png")
            (repo / "reports" / "report.md").write_text(
                "![small](fig.png)\n![external](https://example.com/x.png)\n"
            )

            observed = LocalResourceArtifactReader(repo_root=repo).read_for_association(
                path="reports/report.md",
                role="report",
            )

        self.assertEqual(
            [(figure["link_path"], figure["data"]) for figure in observed["figures"]],
            [("fig.png", b"png")],
        )

    def test_rejects_missing_markdown_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "report.md").write_text("![gone](figures/missing.png)\n")

            with self.assertRaisesRegex(ValidationError, "has no submitted content"):
                LocalResourceArtifactReader(repo_root=repo).read_for_association(
                    path="report.md",
                    role="report",
                )

    def test_rejects_oversized_markdown_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "big.png").write_bytes(b"x" * (MARKDOWN_FIGURE_MAX_BYTES + 1))
            (repo / "report.md").write_text("![big](big.png)\n")

            with self.assertRaisesRegex(ValidationError, "maximum figure size"):
                LocalResourceArtifactReader(repo_root=repo).read_for_association(
                    path="report.md",
                    role="report",
                )

    def test_collects_plan_markdown_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "experiments").mkdir()
            (repo / "experiments" / "diagram.png").write_bytes(b"png")
            (repo / "experiments" / "plan.md").write_text("![arch](diagram.png)\n")

            observed = LocalResourceArtifactReader(repo_root=repo).read_for_association(
                path="experiments/plan.md",
                role="plan",
            )

        self.assertEqual(
            [(figure["link_path"], figure["data"]) for figure in observed["figures"]],
            [("diagram.png", b"png")],
        )

    def test_rejects_absolute_markdown_figure_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "report.md").write_text("![bad](/tmp/plot.png)\n")

            with self.assertRaisesRegex(ValidationError, "repo-relative"):
                LocalResourceArtifactReader(repo_root=repo).read_for_association(
                    path="report.md",
                    role="report",
                )


if __name__ == "__main__":
    unittest.main()
