"""LiteratureService: sectioned living document + papers ledger."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from merv.brain.kernel.state.store import StateStore
from merv.brain.kernel.utils import NotFoundError, ValidationError
from merv.brain.research_core.claims import ClaimService
from merv.brain.research_core.literature import (
    MAX_BODY_BYTES,
    MAX_SECTIONS,
    LiteratureService,
    normalize_paper_identity,
)
from merv.brain.research_core.projects import ProjectService


class FakeUnfurl:
    """PaperUnfurl double: allowlists arxiv.org, returns a canned card."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def allowed(self, url: str) -> bool:
        return "arxiv.org" in url

    def unfurl(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("network down")
        return {
            "url": url,
            "title": "Fetched Title",
            "description": "Fetched description.",
            "authors": ["A. Author", "B. Author"],
            "year": "2026",
        }


class NormalizeIdentityTest(unittest.TestCase):
    def test_arxiv_forms_collapse_to_one_key(self) -> None:
        cases = [
            {"url": "https://arxiv.org/abs/2107.03374"},
            {"url": "https://arxiv.org/abs/2107.03374v2"},
            {"url": "http://www.arxiv.org/pdf/2107.03374.pdf"},
            {"url": "https://export.arxiv.org/abs/2107.03374"},
            {"url": "https://ar5iv.org/abs/2107.03374"},
            {"url": "https://arxiv.org/html/2107.03374v1"},
            {"arxiv_id": "2107.03374"},
            {"arxiv_id": "2107.03374v3"},
        ]
        keys = {normalize_paper_identity(**case)[0] for case in cases}
        self.assertEqual(keys, {"arxiv:2107.03374"})

    def test_legacy_arxiv_id(self) -> None:
        key, kind, url = normalize_paper_identity(arxiv_id="cond-mat/0703470v2")
        self.assertEqual(key, "arxiv:cond-mat/0703470")
        self.assertEqual(kind, "arxiv")
        self.assertEqual(url, "https://arxiv.org/abs/cond-mat/0703470")

    def test_doi_forms(self) -> None:
        direct = normalize_paper_identity(doi="10.1038/S41586-021-03819-2")
        via_url = normalize_paper_identity(url="https://doi.org/10.1038/s41586-021-03819-2")
        dx = normalize_paper_identity(url="http://dx.doi.org/10.1038/s41586-021-03819-2")
        self.assertEqual(direct[0], "doi:10.1038/s41586-021-03819-2")
        self.assertEqual(direct[0], via_url[0])
        self.assertEqual(direct[0], dx[0])
        self.assertEqual(direct[1], "doi")

    def test_url_normalization(self) -> None:
        a = normalize_paper_identity(url="https://Example.com:443/Papers/One?q=X")[0]
        b = normalize_paper_identity(url="http://example.com/Papers/One?q=X")[0]
        self.assertEqual(a, b)
        self.assertEqual(a, "url:example.com/Papers/One?q=X")
        # Path/query case is preserved; only scheme/host fold.
        other = normalize_paper_identity(url="https://example.com/papers/one?q=X")[0]
        self.assertNotEqual(a, other)
        # Trailing-slash-only difference on an empty path collapses.
        self.assertEqual(
            normalize_paper_identity(url="https://example.com")[0],
            normalize_paper_identity(url="https://example.com/")[0],
        )
        # Fragments never reach the key.
        self.assertEqual(
            normalize_paper_identity(url="https://example.com/p#sec2")[0],
            normalize_paper_identity(url="https://example.com/p")[0],
        )

    def test_only_the_schemes_own_default_port_is_dropped(self) -> None:
        # https on port 80 is a different endpoint from plain https.
        self.assertNotEqual(
            normalize_paper_identity(url="https://example.com:80/p")[0],
            normalize_paper_identity(url="https://example.com/p")[0],
        )
        self.assertNotEqual(
            normalize_paper_identity(url="http://example.com:443/p")[0],
            normalize_paper_identity(url="http://example.com/p")[0],
        )
        self.assertEqual(
            normalize_paper_identity(url="http://example.com:80/p")[0],
            normalize_paper_identity(url="https://example.com:443/p")[0],
        )

    def test_rejections(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_paper_identity()
        with self.assertRaises(ValidationError):
            normalize_paper_identity(arxiv_id="not-an-id")
        with self.assertRaises(ValidationError):
            normalize_paper_identity(doi="11.1234/x")
        with self.assertRaises(ValidationError):
            normalize_paper_identity(url="ftp://example.com/p")


class LiteratureServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(db_path=Path(self.tmp.name) / "state.sqlite")
        self.unfurl = FakeUnfurl()
        self.svc = LiteratureService(store=self.store, unfurl=self.unfurl)
        self.claims = ClaimService(store=self.store)
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT id FROM projects").fetchone()
            self.project_id = str(row["id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _section_rows(self) -> int:
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM litreview_sections"
            ).fetchone()
            return int(row["n"])

    # ------------------------------------------------------------- summary

    def test_view_synthesizes_absent_summary_without_creating_it(self) -> None:
        view = self.svc.view(project_id=self.project_id)
        self.assertEqual(view["summary"]["title"], "General Summary")
        self.assertEqual(view["summary"]["revision"], 0)
        self.assertFalse(view["summary"]["exists"])
        self.assertEqual(view["paper_count"], 0)
        self.assertEqual(self._section_rows(), 0)

    def test_first_write_creates_summary_and_cas_guards_it(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="edit", section="summary",
                tldr="x", expected_revision=3,
            )
        result = self.svc.edit(
            project_id=self.project_id, op="edit", section="summary",
            tldr="The field in one line.", body="Longer prose.",
            expected_revision=0,
        )
        self.assertEqual(result["section"]["kind"], "summary")
        self.assertEqual(result["section"]["revision"], 1)
        # Second create attempt is stale.
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="edit", section="summary",
                tldr="again", expected_revision=0,
            )
        updated = self.svc.edit(
            project_id=self.project_id, op="edit", section="summary",
            tldr="Refined.", expected_revision=1,
        )
        self.assertEqual(updated["section"]["revision"], 2)
        self.assertEqual(updated["section"]["body"], "Longer prose.")
        self.assertEqual(updated["section"]["title"], "General Summary")

    def test_summary_cannot_be_deleted(self) -> None:
        self.svc.edit(
            project_id=self.project_id, op="edit", section="summary",
            tldr="x", expected_revision=0,
        )
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="delete", section="summary",
                expected_revision=1,
            )

    # ------------------------------------------------------------ sections

    def _add(self, title: str, tldr: str = "tl;dr") -> dict[str, Any]:
        return self.svc.edit(
            project_id=self.project_id, op="add", title=title, tldr=tldr,
            body=f"Body of {title}.",
        )["section"]

    def test_add_edit_delete_roundtrip_with_cas(self) -> None:
        section = self._add("SFT best practices")
        self.assertEqual(section["revision"], 1)
        self.assertEqual(section["position"], 1)
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="edit", section=section["id"],
                tldr="new", expected_revision=99,
            )
        edited = self.svc.edit(
            project_id=self.project_id, op="edit", section="sft best PRACTICES",
            tldr="Updated tldr.", expected_revision=1,
        )["section"]
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["tldr"], "Updated tldr.")
        deleted = self.svc.edit(
            project_id=self.project_id, op="delete", section=edited["id"],
            expected_revision=2,
        )
        self.assertEqual(deleted["deleted"], edited["id"])
        self.assertEqual(self._section_rows(), 0)

    def test_summary_presents_exists_true_once_written(self) -> None:
        self.svc.edit(
            project_id=self.project_id, op="edit", section="summary",
            tldr="Written.", expected_revision=0,
        )
        view = self.svc.view(project_id=self.project_id)
        self.assertTrue(view["summary"]["exists"])
        self.assertEqual(view["summary"]["revision"], 1)

    def test_summary_title_is_reserved_for_sections(self) -> None:
        for title in ("summary", "General Summary", "GENERAL SUMMARY"):
            with self.assertRaises(ValidationError):
                self.svc.edit(
                    project_id=self.project_id, op="add", title=title, tldr="t"
                )
        section = self.svc.edit(
            project_id=self.project_id, op="add", title="Ok", tldr="t"
        )["section"]
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="edit", section=str(section["id"]),
                title="Summary", expected_revision=1,
            )

    def test_titles_are_casefold_unique_and_required(self) -> None:
        self._add("Token Density")
        with self.assertRaises(ValidationError):
            self._add("token density")
        with self.assertRaises(ValidationError):
            self.svc.edit(project_id=self.project_id, op="add", title="", tldr="x")
        with self.assertRaises(ValidationError):
            self.svc.edit(project_id=self.project_id, op="add", title="No tldr", tldr="")

    def test_miss_returns_outline_for_self_correction(self) -> None:
        self._add("Curriculum ordering")
        with self.assertRaises(NotFoundError) as ctx:
            self.svc.view(project_id=self.project_id, section="wrong name")
        self.assertIn("Curriculum ordering", str(ctx.exception))

    def test_body_byte_cap(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="add", title="Big",
                tldr="x", body="é" * (MAX_BODY_BYTES // 2 + 1),
            )

    def test_section_count_cap(self) -> None:
        for i in range(MAX_SECTIONS):
            self._add(f"Section {i}")
        with self.assertRaises(ValidationError):
            self._add("One too many")

    def test_reorder_requires_fresh_pairs_and_bumps_revisions(self) -> None:
        first = self._add("First")
        second = self._add("Second")
        stale = [
            {"id": second["id"], "revision": 99},
            {"id": first["id"], "revision": first["revision"]},
        ]
        with self.assertRaises(ValidationError):
            self.svc.edit(project_id=self.project_id, op="reorder", order=stale)
        result = self.svc.edit(
            project_id=self.project_id, op="reorder",
            order=[
                {"id": second["id"], "revision": second["revision"]},
                {"id": first["id"], "revision": first["revision"]},
            ],
        )
        self.assertEqual(result["order"], [second["id"], first["id"]])
        view = self.svc.view(project_id=self.project_id)
        self.assertEqual(
            [s["id"] for s in view["sections"]], [second["id"], first["id"]]
        )
        self.assertTrue(all(s["revision"] == 2 for s in view["sections"]))
        # The old pairs are now stale — a concurrent reorder loses cleanly.
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="reorder",
                order=[
                    {"id": first["id"], "revision": 1},
                    {"id": second["id"], "revision": 1},
                ],
            )

    def test_incomplete_reorder_membership_fails(self) -> None:
        first = self._add("Only listed")
        self._add("Missing from order")
        with self.assertRaises(ValidationError):
            self.svc.edit(
                project_id=self.project_id, op="reorder",
                order=[{"id": first["id"], "revision": 1}],
            )

    # -------------------------------------------------------------- papers

    def test_cite_fetches_on_allowlisted_host(self) -> None:
        result = self.svc.cite(
            project_id=self.project_id, url="https://arxiv.org/abs/2107.03374"
        )
        paper = result["paper"]
        self.assertFalse(result["deduplicated"])
        self.assertEqual(paper["fetch_status"], "fetched")
        self.assertEqual(paper["title"], "Fetched Title")
        self.assertEqual(paper["authors"], ["A. Author", "B. Author"])
        self.assertEqual(paper["source_kind"], "arxiv")
        self.assertEqual(paper["norm_key"], "arxiv:2107.03374")

    def test_cite_off_allowlist_is_manual_never_fetched(self) -> None:
        result = self.svc.cite(
            project_id=self.project_id,
            url="https://example.com/paper.pdf",
            title="Manual Title",
        )
        self.assertEqual(result["paper"]["fetch_status"], "manual")
        self.assertEqual(result["paper"]["title"], "Manual Title")
        self.assertEqual(self.unfurl.calls, [])

    def test_cite_deduplicates_across_url_forms(self) -> None:
        first = self.svc.cite(
            project_id=self.project_id, url="https://arxiv.org/abs/2107.03374"
        )
        second = self.svc.cite(
            project_id=self.project_id, url="https://arxiv.org/pdf/2107.03374v2.pdf"
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["paper"]["id"], second["paper"]["id"])
        with closing(self.store.connect()) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()
            self.assertEqual(int(count["n"]), 1)

    def test_fetch_failure_registers_failed_then_upgrades(self) -> None:
        failing = LiteratureService(store=self.store, unfurl=FakeUnfurl(fail=True))
        result = failing.cite(
            project_id=self.project_id, arxiv_id="2107.03374", title="Fallback"
        )
        self.assertEqual(result["paper"]["fetch_status"], "failed")
        self.assertEqual(result["paper"]["title"], "Fallback")
        upgraded = self.svc.cite(project_id=self.project_id, arxiv_id="2107.03374")
        self.assertTrue(upgraded["deduplicated"])
        self.assertEqual(upgraded["paper"]["fetch_status"], "fetched")
        self.assertEqual(upgraded["paper"]["title"], "Fetched Title")
        # A later failed cite never downgrades fetched metadata.
        downgrade = failing.cite(project_id=self.project_id, arxiv_id="2107.03374")
        self.assertEqual(downgrade["paper"]["fetch_status"], "fetched")
        self.assertEqual(downgrade["paper"]["title"], "Fetched Title")

    def test_links_validate_same_project_targets(self) -> None:
        claim = self.claims.create(
            statement="Token density drives quality.", project_id=self.project_id
        )
        section = self._add("Token density")
        result = self.svc.cite(
            project_id=self.project_id, arxiv_id="2107.03374",
            targets=[
                {"type": "claim", "id": claim["id"]},
                {"type": "litreview_section", "id": "Token density"},
            ],
        )
        self.assertEqual(len(result["new_links"]), 2)
        self.assertEqual(result["new_links"][1]["id"], section["id"])
        # Re-citing the same targets is idempotent.
        again = self.svc.cite(
            project_id=self.project_id, arxiv_id="2107.03374",
            targets=[{"type": "claim", "id": claim["id"]}],
        )
        self.assertEqual(again["new_links"], [])
        # A claim from another project reads as not-found.
        other = ProjectService(store=self.store).create(name="Other project")
        foreign = self.claims.create(statement="Foreign.", project_id=other["id"])
        with self.assertRaises(NotFoundError):
            self.svc.cite(
                project_id=self.project_id, arxiv_id="2107.03374",
                targets=[{"type": "claim", "id": foreign["id"]}],
            )
        with self.assertRaises(ValidationError):
            self.svc.cite(
                project_id=self.project_id, arxiv_id="2107.03374",
                targets=[{"type": "sandbox", "id": "sb_1"}],
            )

    def test_deleting_section_removes_its_links(self) -> None:
        section = self._add("Doomed")
        self.svc.cite(
            project_id=self.project_id, arxiv_id="2107.03374",
            targets=[{"type": "litreview_section", "id": section["id"]}],
        )
        self.svc.edit(
            project_id=self.project_id, op="delete", section=section["id"],
            expected_revision=1,
        )
        with closing(self.store.connect()) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM paper_links").fetchone()
            self.assertEqual(int(count["n"]), 0)

    def test_paper_pagination(self) -> None:
        for i in range(3):
            self.svc.cite(
                project_id=self.project_id, url=f"https://example.com/p{i}",
                title=f"P{i}",
            )
        page = self.svc.view(project_id=self.project_id, papers=True, limit=2)
        self.assertEqual(len(page["papers"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        rest = self.svc.view(
            project_id=self.project_id, papers=True, limit=2,
            cursor=page["next_cursor"],
        )
        self.assertEqual(len(rest["papers"]), 1)
        self.assertIsNone(rest["next_cursor"])

    # -------------------------------------------------------------- events

    def test_mutations_record_full_post_state_events(self) -> None:
        section = self._add("Evented")
        self.svc.cite(project_id=self.project_id, arxiv_id="2107.03374")
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT type, target_id, payload_json FROM events "
                "WHERE type LIKE 'litreview.%' ORDER BY id",
            ).fetchall()
        types = [row["type"] for row in rows]
        self.assertEqual(types, ["litreview.section_added", "litreview.paper_cited"])
        self.assertIn('"title": "Evented"', rows[0]["payload_json"])
        self.assertEqual(rows[0]["target_id"], section["id"])
        self.assertIn('"norm_key": "arxiv:2107.03374"', rows[1]["payload_json"])


if __name__ == "__main__":
    unittest.main()
