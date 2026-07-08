"""Blob store contract + gated-role byte capture at associate (plan Phase 1)."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.domain.artifacts import MAX_REPORT_BYTES
from backend.domain.graph_lint import MAX_GRAPH_BYTES
from backend.artifacts.roles import GATED_ROLE_BYTE_CAPS, GATED_ROLES
from backend.storage.blobs import LocalDirBlobStore
from backend.utils import NotFoundError, ValidationError
from tests.fakes import FakeBlobStore


class BlobStoreContractMixin:
    """One behavioral suite run against every BlobStore implementation."""

    def make_store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def test_put_get_round_trip_and_content_addressing(self) -> None:
        store = self.make_store()
        data = b"hello blobs"
        sha = store.put(namespace="proj_a", data=data)
        self.assertEqual(sha, hashlib.sha256(data).hexdigest())
        self.assertEqual(store.get(namespace="proj_a", sha256=sha), data)
        # Idempotent re-put returns the same key.
        self.assertEqual(store.put(namespace="proj_a", data=data), sha)

    def test_namespace_isolation(self) -> None:
        store = self.make_store()
        sha = store.put(namespace="proj_a", data=b"scoped")
        with self.assertRaises(NotFoundError):
            store.get(namespace="proj_b", sha256=sha)
        self.assertIsNone(store.stat(namespace="proj_b", sha256=sha))

    def test_stat_reports_metadata(self) -> None:
        store = self.make_store()
        sha = store.put(namespace="proj_a", data=b"12345", content_type="text/plain")
        stat = store.stat(namespace="proj_a", sha256=sha)
        self.assertIsNotNone(stat)
        self.assertEqual(stat.size_bytes, 5)
        self.assertEqual(stat.content_type, "text/plain")
        self.assertIsNone(stat.expires_at)

    def test_delete(self) -> None:
        store = self.make_store()
        sha = store.put(namespace="proj_a", data=b"gone soon")
        self.assertTrue(store.delete(namespace="proj_a", sha256=sha))
        self.assertFalse(store.delete(namespace="proj_a", sha256=sha))
        with self.assertRaises(NotFoundError):
            store.get(namespace="proj_a", sha256=sha)

    def test_ttl_sweep_with_injected_clock(self) -> None:
        store = self.make_store()
        expiring = store.put(
            namespace="proj_a", data=b"temporary", expires_at="2026-01-01T00:00:00Z"
        )
        keeper = store.put(namespace="proj_a", data=b"permanent")
        later = store.put(
            namespace="proj_a", data=b"later", expires_at="2027-01-01T00:00:00Z"
        )
        swept = store.sweep_expired(now="2026-06-01T00:00:00Z")
        self.assertEqual(swept, 1)
        with self.assertRaises(NotFoundError):
            store.get(namespace="proj_a", sha256=expiring)
        self.assertEqual(store.get(namespace="proj_a", sha256=keeper), b"permanent")
        self.assertEqual(store.get(namespace="proj_a", sha256=later), b"later")

    def test_reput_only_extends_expiry(self) -> None:
        store = self.make_store()
        sha = store.put(
            namespace="proj_a", data=b"pin me", expires_at="2026-01-01T00:00:00Z"
        )
        # Re-put with no expiry pins the blob forever.
        store.put(namespace="proj_a", data=b"pin me")
        self.assertEqual(store.sweep_expired(now="2026-06-01T00:00:00Z"), 0)
        self.assertEqual(store.get(namespace="proj_a", sha256=sha), b"pin me")

    # ---- single-use uploads from off-process producers ----

    def _write_upload(self, target: dict, data: bytes) -> None:
        """PUT bytes to the presigned target the way an off-process producer
        would. Local-mode URLs are file:// staging paths (real presigned
        HTTPS arrives with Phase 8's S3 behind this same seam)."""
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        url = urlsplit(target["url"])
        self.assertEqual(url.scheme, "file")
        Path(url2pathname(url.path)).write_bytes(data)

    def test_presign_put_single_use_round_trip(self) -> None:
        store = self.make_store()
        target = store.presign_put(namespace="proj_a", max_size_bytes=1024)
        self._write_upload(target, b"uploaded bytes")
        stat = store.finalize_put(upload_id=target["upload_id"])
        self.assertEqual(stat.size_bytes, len(b"uploaded bytes"))
        self.assertEqual(stat.namespace, "proj_a")
        self.assertEqual(
            store.get(namespace="proj_a", sha256=stat.sha256), b"uploaded bytes"
        )
        # Single use: the target is consumed by finalize.
        with self.assertRaises(NotFoundError):
            store.finalize_put(upload_id=target["upload_id"])

    def test_presign_get_reads_existing_blob(self) -> None:
        import urllib.request

        store = self.make_store()
        data = b"download me"
        sha = store.put(namespace="proj_a", data=data)
        target = store.presign_get(namespace="proj_a", sha256=sha)

        with urllib.request.urlopen(target["url"]) as response:  # noqa: S310
            self.assertEqual(response.read(), data)

    def test_finalize_enforces_the_size_cap(self) -> None:
        store = self.make_store()
        target = store.presign_put(namespace="proj_a", max_size_bytes=4)
        self._write_upload(target, b"five!")
        with self.assertRaises(ValidationError):
            store.finalize_put(upload_id=target["upload_id"])
        # Consumed either way — a failed finalize cannot be retried.
        with self.assertRaises(NotFoundError):
            store.finalize_put(upload_id=target["upload_id"])

    def test_finalize_without_bytes_raises(self) -> None:
        store = self.make_store()
        target = store.presign_put(namespace="proj_a", max_size_bytes=1024)
        with self.assertRaises(NotFoundError):
            store.finalize_put(upload_id=target["upload_id"])

    def test_finalized_upload_carries_the_presigned_expiry(self) -> None:
        store = self.make_store()
        target = store.presign_put(
            namespace="proj_a", max_size_bytes=1024, expires_at="2026-01-01T00:00:00Z"
        )
        self._write_upload(target, b"ttl-bound")
        stat = store.finalize_put(upload_id=target["upload_id"])
        self.assertEqual(stat.expires_at, "2026-01-01T00:00:00Z")
        # The TTL backstop sweeps an unclaimed object.
        self.assertEqual(store.sweep_expired(now="2026-06-01T00:00:00Z"), 1)
        with self.assertRaises(NotFoundError):
            store.get(namespace="proj_a", sha256=stat.sha256)


class LocalDirBlobStoreTest(BlobStoreContractMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_store(self) -> LocalDirBlobStore:
        return LocalDirBlobStore(root=Path(self.tmp.name) / "blobs")


class FakeBlobStoreTest(BlobStoreContractMixin, unittest.TestCase):
    def make_store(self) -> FakeBlobStore:
        return FakeBlobStore()


class GatedRoleCapAlignmentTest(unittest.TestCase):
    def test_caps_mirror_lint_constants(self) -> None:
        self.assertEqual(GATED_ROLE_BYTE_CAPS["report"], MAX_REPORT_BYTES)
        self.assertEqual(GATED_ROLE_BYTE_CAPS["graph"], MAX_GRAPH_BYTES)
        self.assertEqual(GATED_ROLE_BYTE_CAPS["project_graph"], MAX_GRAPH_BYTES)
        self.assertEqual(GATED_ROLE_BYTE_CAPS["reflection_lens_doc"], 16_000)
        self.assertEqual(GATED_ROLE_BYTE_CAPS["reflection_doc"], 16_000)
        self.assertEqual(GATED_ROLE_BYTE_CAPS["synthesis_doc"], 16_000)
        self.assertEqual(
            GATED_ROLES,
            {
                "plan",
                "report",
                "graph",
                "project_graph",
                "reflection_lens_doc",
                "reflection_doc",
                "synthesis_doc",
                "change_spec",
                "proposals",
                "reflection",
            },
        )


class AssociateByteCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Blob Capture")["id"]
        self.exp_id = self.call(
            "experiment.create",
            name="exp-1",
            project_id=self.project_id,
            intent="Capture gated bytes.",
        )["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _associate(self, *, path: str, role: str):
        resource = self.call(
            "resource.register_file", project_id=self.project_id, path=path
        )
        return self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=resource["id"],
            target_type="experiment",
            target_id=self.exp_id,
            role=role,
        )

    def test_gated_role_associate_captures_blob_keyed_by_version_sha(self) -> None:
        content = b"## Plan\nDo the thing.\n"
        (self.repo / "plan.md").write_bytes(content)
        resource = self._associate(path="plan.md", role="plan")
        sha = resource["current_version"]["content_sha256"]
        self.assertEqual(
            self.app.blobs.get(namespace=self.project_id, sha256=sha), content
        )

    def test_plan_markdown_figures_are_submitted_with_plan(self) -> None:
        (self.repo / "experiments" / "exp-1" / "figures").mkdir(parents=True)
        (self.repo / "experiments" / "exp-1" / "figures" / "diagram.png").write_bytes(
            b"\x89PNG\r\n\x1a\nplan"
        )
        (self.repo / "experiments" / "exp-1" / "plan.md").write_text(
            "## Summary\nPlan with an architecture diagram.\n\n"
            "![architecture](figures/diagram.png)\n\n"
            "## Objective & hypothesis\nTest the plan figure capture path.\n\n"
            "## Evaluation\nSuccess means the submitted figure bytes are retrievable.\n"
        )

        resource = self._associate(path="experiments/exp-1/plan.md", role="plan")
        version_id = resource["current_version_id"]

        self.assertEqual(
            self.app.resources.submitted_figure(
                version_id=version_id,
                link_path="figures/diagram.png",
            ),
            b"\x89PNG\r\n\x1a\nplan",
        )

    def test_result_role_associate_stores_no_blob(self) -> None:
        content = b"big result payload"
        (self.repo / "out.txt").write_bytes(content)
        resource = self._associate(path="out.txt", role="result")
        sha = resource["current_version"]["content_sha256"]
        self.assertIsNone(self.app.blobs.stat(namespace=self.project_id, sha256=sha))

    def test_oversize_gated_artifact_is_rejected_and_not_associated(self) -> None:
        (self.repo / "plan.md").write_bytes(b"x" * (GATED_ROLE_BYTE_CAPS["plan"] + 1))
        with self.assertRaises(ValidationError) as ctx:
            self._associate(path="plan.md", role="plan")
        self.assertIn("maximum", ctx.exception.message)
        resource = self.call(
            "resource.register_file", project_id=self.project_id, path="plan.md"
        )
        self.assertEqual(resource["associations"], [])

    def test_invalid_association_intent_preflights_before_reading_artifact(self) -> None:
        path = self.repo / "plan.md"
        path.write_text("valid enough to register\n")
        resource = self.call(
            "resource.register_file", project_id=self.project_id, path="plan.md"
        )
        path.unlink()

        with self.assertRaisesRegex(NotFoundError, "experiment not found"):
            self.call(
                "resource.associate",
                project_id=self.project_id,
                resource_id=resource["id"],
                target_type="experiment",
                target_id="exp_missing",
                role="plan",
            )

    def test_live_file_gate_semantics_unchanged_this_phase(self) -> None:
        # Phase 1 is additive: associating still re-observes the live file
        # (capture happens alongside, not instead).
        (self.repo / "plan.md").write_text("v1\n")
        first = self._associate(path="plan.md", role="plan")
        (self.repo / "plan.md").write_text("v2 — edited after first associate\n")
        second = self._associate(path="plan.md", role="plan")
        self.assertNotEqual(
            first["current_version_id"], second["current_version_id"]
        )
        for version in (first, second):
            sha = version["current_version"]["content_sha256"]
            self.assertIsNotNone(
                self.app.blobs.stat(namespace=self.project_id, sha256=sha)
            )

if __name__ == "__main__":
    unittest.main()
