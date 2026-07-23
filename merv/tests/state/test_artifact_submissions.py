from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from merv.brain.artifacts.submissions import (
    ArtifactSubmissionService,
    UPLOAD_TOKEN_TTL_SECONDS,
)
from merv.brain.kernel.state import StateStore
from merv.brain.kernel.utils import (
    NotFoundError,
    ValidationError,
    WorkflowError,
    new_id,
    now_iso,
)
from merv.brain.object_storage.blobs import LocalDirBlobStore
from merv.brain.research_core.association_targets import AssociationTargets
from merv.brain.research_core.domain.review_snapshot import review_snapshot_id

PLAN_MD = "## Summary\nBody.\n\n## Objective\nGoal.\n\n## Evaluation\nMetric.\n"
REPORT_WITH_FIGURE = (
    "## Summary\nRan it.\n\n## Results\n![curve](figures/curve.png)\n\n"
    "## Deviations from plan\nNone.\n\n## Conclusion\nDone.\n"
)


class ArtifactSubmissionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = StateStore(db_path=root / "state.sqlite")
        self.blobs = LocalDirBlobStore(root=root / "blobs")
        self.service = ArtifactSubmissionService(
            store=self.store,
            blobs=self.blobs,
            association_targets=AssociationTargets(store=self.store),
        )
        with closing(self.store.connect()) as conn:
            self.project_id = str(conn.execute("SELECT id FROM projects").fetchone()["id"])
        self.experiment_id = self._insert_experiment()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_experiment(self, *, attempt_index: int = 1) -> str:
        experiment_id = new_id(prefix="exp")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO experiments
                  (id, project_id, name, intent, status, attempt_index,
                   revision_context, created_at, updated_at)
                VALUES (?, ?, ?, 'test', 'planned', ?, '', ?, ?)
                """,
                (experiment_id, self.project_id, experiment_id, attempt_index,
                 now_iso(), now_iso()),
            )
        return experiment_id

    def _submit(self, *, role: str = "plan", path: str = "plan.md", **kwargs):
        return self.service.submit(
            target_type="experiment",
            target_id=self.experiment_id,
            role=role,
            path=path,
            project_id=self.project_id,
            **kwargs,
        )

    def _token(self, pending: dict) -> str:
        return pending["run"].rsplit("/", 1)[-1].rstrip("'")

    def test_full_loop_submit_upload_read(self) -> None:
        pending = self._submit()
        self.assertTrue(pending["artifact_id"].startswith("art_"))
        self.assertIn("/api/artifacts/u/", pending["run"])

        result = self.service.complete_upload(
            token=self._token(pending), data=PLAN_MD.encode()
        )
        self.assertEqual(result["artifact_id"], pending["artifact_id"])
        self.assertEqual(result["figures"], [])

        evidence = self.service.artifacts_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].role, "plan")
        self.assertEqual(evidence[0].attempt_index, 1)
        self.assertEqual(evidence[0].path, "plan.md")

        document = self.service.submitted_document(
            artifact_id=pending["artifact_id"], what="experiment plan"
        )
        self.assertEqual(document.text, PLAN_MD)
        self.assertEqual(document.role, "plan")

    def test_submit_validates_role_and_lens_pairing(self) -> None:
        with self.assertRaises(ValidationError):
            self._submit(role="code", path="train.py")  # untyped roles are gone
        with self.assertRaises(ValidationError):
            self._submit(role="reflection_lens_doc", path="rigor.md")  # no lens_id
        with self.assertRaises(ValidationError):
            self._submit(role="plan", lens_id="rigor")  # lens_id on wrong role

    def test_upload_token_is_single_use(self) -> None:
        pending = self._submit()
        token = self._token(pending)
        self.service.complete_upload(token=token, data=PLAN_MD.encode())
        with self.assertRaises(NotFoundError):
            self.service.complete_upload(token=token, data=PLAN_MD.encode())

    def test_expired_pending_rows_are_swept_on_access(self) -> None:
        pending = self._submit()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (pending["artifact_id"],),
            )
        with self.assertRaises(NotFoundError):
            self.service.complete_upload(
                token=self._token(pending), data=PLAN_MD.encode()
            )
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM artifacts WHERE id = ?", (pending["artifact_id"],)
            ).fetchone()
        self.assertIsNone(row)
        self.assertGreater(UPLOAD_TOKEN_TTL_SECONDS, 0)

    def test_oversize_upload_is_rejected_with_cap_details(self) -> None:
        pending = self._submit()
        with self.assertRaises(ValidationError) as caught:
            self.service.complete_upload(
                token=self._token(pending), data=b"x" * 20_000
            )
        self.assertEqual(caught.exception.details["max_bytes"], 16_000)
        # The token died with the pending row? No — the row survives a failed
        # cap check inside the rolled-back transaction, so a slimmed retry works.
        result = self.service.complete_upload(
            token=self._token(pending), data=PLAN_MD.encode()
        )
        self.assertEqual(result["artifact_id"], pending["artifact_id"])

    def test_resubmit_supersedes_and_invalidates_the_snapshot(self) -> None:
        first = self._submit()
        self.service.complete_upload(token=self._token(first), data=PLAN_MD.encode())
        snapshot_before = self._snapshot()

        second = self._submit()
        self.service.complete_upload(
            token=self._token(second), data=(PLAN_MD + "v2\n").encode()
        )
        evidence = self.service.artifacts_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        self.assertEqual(
            [item.artifact_id for item in evidence], [second["artifact_id"]]
        )
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])
        self.assertNotEqual(snapshot_before, self._snapshot())

    def _snapshot(self) -> str:
        evidence = self.service.artifacts_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        return review_snapshot_id(
            target_type="experiment",
            target={
                "id": self.experiment_id,
                "status": "planned",
                "attempt_index": 1,
                "current_attempt_artifacts": [
                    {
                        "id": item.artifact_id,
                        "role": item.role,
                        "attempt_index": item.attempt_index,
                    }
                    for item in evidence
                ],
            },
        )

    def test_figure_flow_mints_tokens_and_pins_bytes(self) -> None:
        pending = self._submit(role="report", path="report.md")
        result = self.service.complete_upload(
            token=self._token(pending), data=REPORT_WITH_FIGURE.encode()
        )
        self.assertEqual(
            [figure["link_path"] for figure in result["figures"]],
            ["figures/curve.png"],
        )
        # Until the figure lands, the document reports no submitted figures.
        document = self.service.submitted_document(
            artifact_id=pending["artifact_id"], what="results report"
        )
        self.assertEqual(document.figure_links, ())

        png = b"\x89PNG fake bytes"
        uploaded = self.service.complete_figure_upload(
            token=result["figures"][0]["token"], data=png
        )
        self.assertEqual(uploaded["link_path"], "figures/curve.png")
        document = self.service.submitted_document(
            artifact_id=pending["artifact_id"], what="results report"
        )
        self.assertEqual(document.figure_links, ("figures/curve.png",))
        self.assertEqual(
            self.service.figure_bytes(
                artifact_id=pending["artifact_id"],
                link_path="figures/curve.png",
                project_id=self.project_id,
            ),
            png,
        )
        with self.assertRaises(NotFoundError):  # figure tokens are single-use
            self.service.complete_figure_upload(
                token=result["figures"][0]["token"], data=png
            )

    def test_result_role_pins_and_feeds_metric_sources(self) -> None:
        pending = self._submit(role="result", path="anything/output.txt")
        self.service.complete_upload(
            token=self._token(pending), data=b'{"accuracy": 0.9}'
        )
        sources = self.service.metric_sources(
            target_id=self.experiment_id, attempt_index=1
        )
        # Path label is a hint, never a gate: a .txt result still parses.
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["data"], {"accuracy": 0.9})
        self.assertEqual(sources[0]["artifact_id"], pending["artifact_id"])

    def test_pin_system_artifact_inserts_complete_exhibit(self) -> None:
        pinned = self.service.pin_system_artifact(
            path="experiments/test/metrics_exhibit.json",
            target_type="experiment",
            target_id=self.experiment_id,
            role="exhibit",
            content_bytes=b'{"kind": "metrics_exhibit"}',
            title="Metrics exhibit",
            project_id=self.project_id,
        )
        evidence = self.service.artifacts_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        self.assertEqual(evidence[0].role, "exhibit")
        self.assertEqual(evidence[0].created_by, "system")
        self.assertEqual(evidence[0].path, "experiments/test/metrics_exhibit.json")
        # Re-pinning replaces in place (same slot, fresh id).
        repinned = self.service.pin_system_artifact(
            path="experiments/test/metrics_exhibit.json",
            target_type="experiment",
            target_id=self.experiment_id,
            role="exhibit",
            content_bytes=b'{"kind": "metrics_exhibit", "v": 2}',
            title="Metrics exhibit",
            project_id=self.project_id,
        )
        evidence = self.service.artifacts_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        self.assertEqual(
            [item.artifact_id for item in evidence], [repinned["artifact_id"]]
        )
        self.assertNotEqual(pinned["artifact_id"], repinned["artifact_id"])

    def test_submitted_document_requires_a_complete_artifact(self) -> None:
        with self.assertRaises(WorkflowError):
            self.service.submitted_document(artifact_id="", what="experiment plan")
        pending = self._submit()
        with self.assertRaises(WorkflowError):
            self.service.submitted_document(
                artifact_id=pending["artifact_id"], what="experiment plan"
            )

    def test_find_lists_only_complete_artifacts(self) -> None:
        pending = self._submit()
        listing = self.service.find(project_id=self.project_id)
        self.assertEqual(listing["count"], 0)
        self.service.complete_upload(token=self._token(pending), data=PLAN_MD.encode())
        listing = self.service.find(
            project_id=self.project_id,
            target_type="experiment",
            target_id=self.experiment_id,
        )
        self.assertEqual(listing["count"], 1)
        self.assertNotIn("upload_token", listing["artifacts"][0])


if __name__ == "__main__":
    unittest.main()
