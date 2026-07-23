"""litreview.* tool contracts and end-to-end dispatch."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.tools.contracts import TOOL_MANIFEST
from merv.brain.surface.tools.tool_facade import ToolValidationError

from tests.support.brain import TestBrain


class _OfflineUnfurl:
    def allowed(self, url: str) -> bool:
        return False

    def unfurl(self, url: str) -> dict:
        raise AssertionError("tool tests must not fetch")


class LitreviewToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=repo,
            db_path=repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.project_id = self.app.current_project()["project"]["id"]
        # Keep tool tests offline: the port double never fetches, so cite
        # registers papers as manual (the fetch path is unit-tested with a
        # richer fake in tests/state/test_literature.py).
        self.app.literature.unfurl = _OfflineUnfurl()

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _call(self, name: str, **kwargs) -> dict:
        return self.app.call_tool(name, {"project_id": self.project_id, **kwargs})

    def test_contract_placement(self) -> None:
        for name in ("litreview.view", "litreview.edit", "litreview.cite"):
            tool = TOOL_MANIFEST[name]
            self.assertEqual(tool.plane, "control", name)
            self.assertEqual(tool.visibility, "public", name)
            self.assertEqual(tool.scope_strategy, "linked-project", name)

    def test_view_edit_cite_roundtrip(self) -> None:
        empty = self._call("litreview.view")
        self.assertFalse(empty["summary"]["exists"])
        self.assertEqual(empty["sections"], [])

        added = self._call(
            "litreview.edit",
            op="add",
            title="SFT best practices",
            tldr="What we know about SFT.",
            body="Long-form notes.",
        )
        section_id = added["section"]["id"]

        cited = self._call(
            "litreview.cite",
            arxiv_id="2107.03374",
            targets=[{"type": "litreview_section", "id": section_id}],
            note="Grounding paper.",
            title="Evaluating LLMs Trained on Code",
        )
        self.assertEqual(cited["paper"]["fetch_status"], "manual")
        self.assertEqual(cited["paper"]["norm_key"], "arxiv:2107.03374")

        full = self._call("litreview.view", section="SFT best practices")
        self.assertEqual(full["section"]["id"], section_id)
        self.assertEqual(len(full["section"]["cited_papers"]), 1)

        page = self._call("litreview.view", papers=True)
        self.assertEqual(len(page["papers"]), 1)
        self.assertEqual(page["papers"][0]["links"][0]["target_id"], section_id)

    def test_contract_bounds_reject_before_dispatch(self) -> None:
        with self.assertRaises(ToolValidationError):
            self._call("litreview.edit", op="add", tldr="x")  # missing title
        with self.assertRaises(ToolValidationError):
            self._call("litreview.edit", op="edit", section="s")  # no revision
        with self.assertRaises(ToolValidationError):
            self._call(
                "litreview.edit", op="add", title="Big", tldr="x", body="é" * 8_001
            )
        with self.assertRaises(ToolValidationError):
            self._call("litreview.cite")  # no identity
        with self.assertRaises(ToolValidationError):
            self._call(  # conflicting identities
                "litreview.cite", url="https://example.com/p", arxiv_id="2107.03374"
            )
        with self.assertRaises(ToolValidationError):
            self._call(
                "litreview.cite",
                url="https://example.com/p",
                targets=[{"type": "claim", "id": "c"}] * 21,
            )
        with self.assertRaises(ToolValidationError):
            self._call("litreview.view", limit=51)


class LitreviewNudgeTest(LitreviewToolsTest):
    def test_status_hint_fires_at_three_unreviewed_papers(self) -> None:
        claim = self.app.call_tool(
            "claim.create",
            {"project_id": self.project_id, "statement": "Papers accumulate."},
        )
        for i in range(2):
            self._call(
                "litreview.cite", url=f"https://example.com/p{i}", title=f"P{i}",
                targets=[{"type": "claim", "id": claim["id"]}],
            )
        status = self._call("workflow.status_and_next")
        self.assertNotIn("litreview", status)
        self._call(
            "litreview.cite", url="https://example.com/p2", title="P2",
            targets=[{"type": "claim", "id": claim["id"]}],
        )
        status = self._call("workflow.status_and_next")
        self.assertEqual(status["litreview"]["papers_unreviewed"], 3)
        self.assertIn("litreview.edit", status["litreview"]["hint"])
        # Working one paper into a section clears it below the threshold.
        section = self._call(
            "litreview.edit", op="add", title="Covered", tldr="t",
        )["section"]
        self._call(
            "litreview.cite", url="https://example.com/p2",
            targets=[{"type": "litreview_section", "id": section["id"]}],
        )
        status = self._call("workflow.status_and_next")
        self.assertNotIn("litreview", status)


if __name__ == "__main__":
    unittest.main()
