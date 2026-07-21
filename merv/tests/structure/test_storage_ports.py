"""Focused guards for submitted bytes and produced-object boundaries."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from typing import get_type_hints, is_typeddict

from merv.brain.application.ports.storage import ProducedObject, ProducedObjectCatalog

from merv.brain.kernel.ports.blob_store import (
    BlobDownloadTarget,
    BlobStat,
    BlobStore,
    BlobTransferStore,
    BlobUploadTarget,
    EvidenceBlobStore,
    ExpiringBlobStore,
    validate_blob_keys,
)
from merv.brain.kernel.ports.object_store import DownloadTarget, ObjectStore, UploadTarget
from merv.brain.kernel.utils import ValidationError
from merv.brain.object_storage import blobs as local_adapter
from merv.brain.object_storage import s3_blobs as s3_adapter
from merv.brain.object_storage import s3_object_store as heavy_s3_adapter
from merv.brain.research_core.experiments import ExperimentService


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "merv" / "brain"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))
    return imports


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))
    return imports


class SubmittedBlobPortTest(unittest.TestCase):
    def test_old_adapter_imports_are_identity_preserving(self) -> None:
        self.assertIs(local_adapter.BlobStat, BlobStat)
        self.assertIs(local_adapter.BlobStore, BlobStore)
        self.assertIs(local_adapter.BlobUploadTarget, BlobUploadTarget)
        self.assertIs(local_adapter.BlobDownloadTarget, BlobDownloadTarget)

        self.assertIs(s3_adapter.BlobStat, BlobStat)

    def test_capabilities_are_narrow_and_transfer_targets_are_typed(self) -> None:
        evidence_methods = {
            name
            for name, value in EvidenceBlobStore.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        expiry_methods = {
            name
            for name, value in ExpiringBlobStore.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        transfer_methods = {
            name
            for name, value in BlobTransferStore.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(evidence_methods, {"put", "get", "stat"})
        self.assertEqual(expiry_methods, {"sweep_expired"})
        self.assertEqual(
            transfer_methods,
            {"presign_get", "delete", "presign_put", "finalize_put"},
        )
        self.assertTrue(is_typeddict(BlobUploadTarget))
        self.assertTrue(is_typeddict(BlobDownloadTarget))
        self.assertEqual(
            set(BlobUploadTarget.__required_keys__),
            {"upload_id", "url", "max_size_bytes", "expires_at"},
        )
        self.assertEqual(set(BlobDownloadTarget.__required_keys__), {"url"})


class HeavyObjectPortTest(unittest.TestCase):
    def test_transfer_targets_are_typed_without_changing_wire_shapes(self) -> None:
        self.assertTrue(is_typeddict(UploadTarget))
        self.assertTrue(is_typeddict(DownloadTarget))
        self.assertEqual(set(UploadTarget.__required_keys__), {"upload_id"})
        self.assertEqual(set(DownloadTarget.__required_keys__), {"url"})
        hints = get_type_hints(ObjectStore)
        self.assertEqual(hints, {})
        self.assertIs(
            get_type_hints(ObjectStore.presign_upload)["return"], UploadTarget
        )
        self.assertIs(
            get_type_hints(ObjectStore.presign_download)["return"], DownloadTarget
        )

    def test_validation_is_kernel_owned_and_behavior_compatible(self) -> None:
        validate_blob_keys(namespace="proj_valid-1", sha256="a" * 64)
        for namespace in ("", "not/a/namespace", "white space"):
            with self.subTest(namespace=namespace), self.assertRaises(ValidationError):
                validate_blob_keys(namespace=namespace)
        with self.assertRaises(ValidationError):
            validate_blob_keys(namespace="proj_valid", sha256="ABC")


class StorageImportBoundaryTest(unittest.TestCase):
    def test_artifacts_and_feed_do_not_import_object_storage(self) -> None:
        paths = (
            SRC_ROOT / "artifacts" / "resources.py",
            SRC_ROOT / "feed" / "feed.py",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(SRC_ROOT)):
                self.assertFalse(
                    any("object_storage" in imported for imported in _imports(path)),
                    _imports(path),
                )

    def test_bootstrap_types_do_not_come_from_blob_adapter(self) -> None:
        paths = (
            SRC_ROOT / "application" / "maintenance.py",
            SRC_ROOT / "surface" / "config.py",
            SRC_ROOT / "surface" / "composition" / "control_mode.py",
            SRC_ROOT / "surface" / "control" / "control_app.py",
            SRC_ROOT / "surface" / "control" / "record_core.py",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(SRC_ROOT)):
                imports = _top_level_imports(path)
                self.assertNotIn("...object_storage.blobs", imports)
                self.assertNotIn("..object_storage.blobs", imports)


class ProducedObjectCatalogBoundaryTest(unittest.TestCase):
    def test_application_owns_batch_catalog_and_research_has_no_storage_seam(self) -> None:
        init_hints = get_type_hints(ExperimentService.__init__)
        self.assertNotIn("storage_objects_reader", init_hints)
        self.assertFalse((SRC_ROOT / "research_core" / "storage_objects.py").exists())
        self.assertNotIn(
            "storage_objects",
            (SRC_ROOT / "research_core" / "experiments.py").read_text(
                encoding="utf-8"
            ),
        )

        call_hints = get_type_hints(ProducedObjectCatalog.by_experiment)
        self.assertEqual(call_hints["project_id"], str)
        self.assertEqual(call_hints["experiment_ids"], tuple[str, ...])
        self.assertEqual(call_hints["return"], dict[str, list[ProducedObject]])
        self.assertEqual(
            list(inspect.signature(ProducedObjectCatalog.by_experiment).parameters),
            ["self", "project_id", "experiment_ids"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
