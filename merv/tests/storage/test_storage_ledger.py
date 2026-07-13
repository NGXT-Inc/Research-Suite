from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from tests.fakes import FakeObjectStore
from backend.control.storage_quotas import StorageQuotaService
from backend.state.store import StateStore
from backend.storage.service import STORAGE_DEFAULT_TTL_SECONDS, StorageLedgerService
from backend.utils import NotFoundError, PermissionDeniedError, ValidationError, parse_iso


class StorageLedgerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StateStore(db_path=self.root / "state.sqlite")
        self.objects = FakeObjectStore()
        self.service = StorageLedgerService(
            store=self.store,
            objects=self.objects,
            blob_quotas=StorageQuotaService(),
        )
        conn = self.store.connect()
        try:
            self.project_id = str(conn.execute("SELECT id FROM projects LIMIT 1").fetchone()["id"])
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_content_upload_complete_available_with_ledger_expiry(self) -> None:
        data = b"important dataset"
        before = datetime.now(UTC)
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/train.tar",
            kind="dataset",
            sha256=self._sha(data),
            size_bytes=len(data),
            content_type="application/x-tar",
        )
        self.assertEqual(registered["object"]["status"], "uploading")
        self.assertGreater(parse_iso(registered["object"]["expires_at"]), before)
        self.assertIn("upload", registered)

        self._write_upload(registered["upload"], data)
        obj = self.service.complete_upload(
            project_id=self.project_id, upload_id=registered["upload"]["upload_id"]
        )

        self.assertEqual(obj["status"], "available")
        self.assertEqual(obj["size_bytes"], len(data))
        self.assertEqual(obj["content_type"], "application/x-tar")
        self.assertIsNotNone(obj["last_accessed_at"])
        expires_at = parse_iso(obj["expires_at"])
        self.assertIsNotNone(expires_at)
        ttl = (expires_at - before).total_seconds()
        self.assertGreater(ttl, STORAGE_DEFAULT_TTL_SECONDS - 120)
        self.assertLess(ttl, STORAGE_DEFAULT_TTL_SECONDS + 120)
        stat = self.objects.stat(namespace=self.project_id, sha256=self._sha(data))
        self.assertIsNotNone(stat)

    def test_physical_dedup_and_idempotent_same_name_sha(self) -> None:
        data = b"shared model weights"
        first = self._put_and_complete(name="models/base.pt", kind="model", data=data)

        deduped = self.service.put_object(
            project_id=self.project_id,
            name="models/base-copy.pt",
            kind="model",
            sha256=self._sha(data),
            size_bytes=len(data),
        )
        self.assertTrue(deduped["deduped"])
        self.assertNotIn("upload", deduped)
        self.assertEqual(deduped["object"]["status"], "available")
        self.assertEqual(deduped["object"]["version"], 1)

        again = self.service.put_object(
            project_id=self.project_id,
            name="models/base-copy.pt",
            kind="model",
            sha256=self._sha(data),
            size_bytes=len(data),
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["object"]["id"], deduped["object"]["id"])
        self.assertEqual(first["content_sha256"], deduped["object"]["content_sha256"])

    def test_blob_budget_reserves_pending_uploads_and_releases_on_delete(self) -> None:
        self._set_blob_budget(8)
        reserved = self.service.put_object(
            project_id=self.project_id,
            name="datasets/reserved.bin",
            kind="dataset",
            sha256=self._sha(b"12345"),
            size_bytes=5,
        )

        with self.assertRaises(PermissionDeniedError) as ctx:
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/over.bin",
                kind="dataset",
                sha256=self._sha(b"6789"),
                size_bytes=4,
            )

        self.assertEqual(ctx.exception.details["quota"], "blob_bytes_budget")
        self.assertEqual(ctx.exception.details["used"], 5)
        self.service.delete(
            project_id=self.project_id, object_id=reserved["object"]["id"]
        )
        admitted = self.service.put_object(
            project_id=self.project_id,
            name="datasets/over.bin",
            kind="dataset",
            sha256=self._sha(b"6789"),
            size_bytes=4,
        )
        self.assertEqual(admitted["object"]["status"], "uploading")

    def test_expired_pending_upload_is_aborted_and_releases_quota(self) -> None:
        self._set_blob_budget(5)
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/abandoned.bin",
            kind="dataset",
            sha256=self._sha(b"12345"),
            size_bytes=5,
        )
        upload_id = registered["upload"]["upload_id"]
        self._write_upload(registered["upload"], b"12345")

        self.assertEqual(
            self.service.sweep_expired(
                now=registered["object"]["expires_at"]
            ),
            1,
        )

        expired = self.service.get_object(
            project_id=self.project_id, object_id=registered["object"]["id"]
        )["object"]
        self.assertEqual(expired["status"], "expired")
        self.assertNotIn(upload_id, self.objects.uploads)
        replacement = self.service.put_object(
            project_id=self.project_id,
            name="datasets/replacement.bin",
            kind="dataset",
            sha256=self._sha(b"abcde"),
            size_bytes=5,
        )
        self.assertEqual(replacement["object"]["status"], "uploading")

    def test_blob_budget_charges_content_dedup_once(self) -> None:
        data = b"shared"
        self._set_blob_budget(len(data))
        self._put_and_complete(name="models/one.bin", kind="model", data=data)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE tenant_quotas SET blob_bytes_budget = 1 WHERE tenant_id = 'local'"
            )

        deduped = self.service.put_object(
            project_id=self.project_id,
            name="models/two.bin",
            kind="model",
            sha256=self._sha(data),
            size_bytes=len(data),
        )

        self.assertTrue(deduped["deduped"])
        with self.assertRaises(PermissionDeniedError):
            self.service.put_object(
                project_id=self.project_id,
                name="models/other.bin",
                kind="model",
                sha256=self._sha(b"x"),
                size_bytes=1,
            )

    def test_blob_budget_charges_each_pending_upload_for_same_content(self) -> None:
        data = b"12345"
        self._set_blob_budget(len(data))
        first = self.service.put_object(
            project_id=self.project_id,
            name="datasets/pending-one.bin",
            kind="dataset",
            sha256=self._sha(data),
            size_bytes=len(data),
        )

        with self.assertRaises(PermissionDeniedError) as ctx:
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/pending-two.bin",
                kind="dataset",
                sha256=self._sha(data),
                size_bytes=len(data),
            )

        self.assertEqual(ctx.exception.details["used"], len(data))
        self.assertEqual(ctx.exception.details["requested"], len(data))
        self.assertEqual(set(self.objects.uploads), {first["upload"]["upload_id"]})

    def test_blob_budget_reservations_are_atomic(self) -> None:
        self._set_blob_budget(6)

        def reserve(name: str, data: bytes) -> bool:
            service = StorageLedgerService(
                store=self.store,
                objects=FakeObjectStore(),
                blob_quotas=StorageQuotaService(),
            )
            try:
                service.put_object(
                    project_id=self.project_id,
                    name=name,
                    kind="dataset",
                    sha256=self._sha(data),
                    size_bytes=len(data),
                )
            except PermissionDeniedError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = list(
                pool.map(reserve, ("datasets/a.bin", "datasets/b.bin"), (b"aaaa", b"bbbb"))
            )

        self.assertEqual(sorted(admitted), [False, True])
        conn = self.store.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM storage_objects WHERE status = 'uploading'"
            ).fetchone()["count"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_failed_upload_abort_keeps_quota_reserved_until_cleanup(self) -> None:
        self._set_blob_budget(5)
        hooked = _HookedObjectStore(self.objects)
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/cancel.bin",
            kind="dataset",
            sha256=self._sha(b"12345"),
            size_bytes=5,
        )
        upload_id = registered["upload"]["upload_id"]
        staged = self.objects.uploads[upload_id]["path"]
        self._write_upload(registered["upload"], b"12345")
        hooked.abort_error = RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.service.delete(
                project_id=self.project_id,
                object_id=registered["object"]["id"],
            )

        row = self.service.get_object(
            project_id=self.project_id, object_id=registered["object"]["id"]
        )["object"]
        self.assertEqual(row["status"], "uploading")
        self.assertIn(upload_id, self.objects.uploads)
        self.assertTrue(staged.exists())
        with self.assertRaises(PermissionDeniedError):
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/blocked.bin",
                kind="dataset",
                sha256=self._sha(b"x"),
                size_bytes=1,
            )

        hooked.abort_error = None
        self.service.delete(
            project_id=self.project_id,
            object_id=registered["object"]["id"],
        )
        self.assertNotIn(upload_id, self.objects.uploads)
        self.assertFalse(staged.exists())
        admitted = self.service.put_object(
            project_id=self.project_id,
            name="datasets/replacement.bin",
            kind="dataset",
            sha256=self._sha(b"abcde"),
            size_bytes=5,
        )
        self.assertEqual(admitted["object"]["status"], "uploading")

    def test_checksum_rejection_releases_consumed_upload_quota(self) -> None:
        expected = b"right!"
        self._set_blob_budget(len(expected))
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/rejected.bin",
            kind="dataset",
            sha256=self._sha(expected),
            size_bytes=len(expected),
        )
        upload_id = registered["upload"]["upload_id"]
        self._write_upload(registered["upload"], b"wrong!")

        with self.assertRaisesRegex(ValidationError, "checksum mismatch"):
            self.service.complete_upload(
                project_id=self.project_id, upload_id=upload_id
            )

        rejected = self.service.get_object(
            project_id=self.project_id, object_id=registered["object"]["id"]
        )["object"]
        self.assertEqual(rejected["status"], "deleted")
        self.assertNotIn(upload_id, self.objects.uploads)
        replacement = self.service.put_object(
            project_id=self.project_id,
            name="datasets/replacement.bin",
            kind="dataset",
            sha256=self._sha(b"valid!"),
            size_bytes=6,
        )
        self.assertEqual(replacement["object"]["status"], "uploading")

    def test_transient_completion_failure_remains_retryable(self) -> None:
        hooked = _HookedObjectStore(self.objects)
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/retry.bin",
            kind="dataset",
            sha256=self._sha(b"retry"),
            size_bytes=5,
        )

        def fail_transiently() -> None:
            raise RuntimeError("provider unavailable")

        hooked.before_complete = fail_transiently
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.service.complete_upload(
                project_id=self.project_id,
                upload_id=registered["upload"]["upload_id"],
            )

        pending = self.service.get_object(
            project_id=self.project_id, object_id=registered["object"]["id"]
        )["object"]
        self.assertEqual(pending["status"], "uploading")
        self.assertIn(registered["upload"]["upload_id"], self.objects.uploads)

    def test_version_auto_increments_for_new_content_same_name(self) -> None:
        first = self._put_and_complete(name="datasets/train.tar", kind="dataset", data=b"v1")
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/train.tar",
            kind="dataset",
            sha256=self._sha(b"v2"),
            size_bytes=2,
        )
        self.assertEqual(first["version"], 1)
        self.assertEqual(registered["object"]["version"], 2)
        self._write_upload(registered["upload"], b"v2")
        second = self.service.complete_upload(
            project_id=self.project_id, upload_id=registered["upload"]["upload_id"]
        )
        self.assertEqual(second["version"], 2)

    def test_list_filters_pagination_and_compact(self) -> None:
        self._put_and_complete(name="datasets/a.tar", kind="dataset", data=b"a")
        self._put_and_complete(name="models/a.pt", kind="model", data=b"b")
        self._put_and_complete(name="datasets/b.tar", kind="dataset", data=b"c")

        datasets = self.service.list_objects(project_id=self.project_id, kind="dataset")
        self.assertEqual(datasets["total"], 2)
        self.assertEqual({obj["kind"] for obj in datasets["objects"]}, {"dataset"})

        named = self.service.list_objects(project_id=self.project_id, name="models/a.pt")
        self.assertEqual(named["total"], 1)
        self.assertEqual(named["objects"][0]["name"], "models/a.pt")

        page = self.service.list_objects(
            project_id=self.project_id, status="available", limit=2, offset=0, compact=True
        )
        self.assertEqual(page["returned"], 2)
        self.assertEqual(page["total"], 3)
        self.assertTrue(page["has_more"])
        self.assertNotIn("notes", page["objects"][0])

    def test_default_list_only_returns_available_objects(self) -> None:
        available = self._put_and_complete(
            name="datasets/ready.tar", kind="dataset", data=b"ready"
        )
        expired = self._put_and_complete(
            name="datasets/expired.tar", kind="dataset", data=b"expired"
        )
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/pending.tar",
            kind="dataset",
            sha256=self._sha(b"pending"),
            size_bytes=len(b"pending"),
        )
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE storage_objects SET status = 'expired' WHERE id = ?",
                (expired["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        listed = self.service.list_objects(project_id=self.project_id)
        with_expired = self.service.list_objects(
            project_id=self.project_id, include_expired=True
        )
        uploading = self.service.list_objects(project_id=self.project_id, status="uploading")

        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["objects"][0]["id"], available["id"])
        self.assertEqual(
            {obj["id"] for obj in with_expired["objects"]},
            {available["id"], expired["id"]},
        )
        self.assertEqual(uploading["total"], 1)
        self.assertEqual(uploading["objects"][0]["id"], registered["object"]["id"])
        self.assertTrue(
            any(
                "logs or traces over about 10 MB" in item
                for item in listed["guidance"]["use_storage_for"]
            )
        )
        self.assertIn("metrics TSV/JSON", " ".join(listed["guidance"]["keep_as_resources"]))

    def test_resolve_latest_download_and_extend_only_access_touch(self) -> None:
        self._put_and_complete(name="models/latest.pt", kind="model", data=b"old")
        new = self._put_and_complete(name="models/latest.pt", kind="model", data=b"new")
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE storage_objects SET expires_at = ?, last_accessed_at = NULL WHERE id = ?",
                ("2026-01-01T00:00:00Z", new["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        resolved = self.service.resolve(project_id=self.project_id, name="models/latest.pt")

        self.assertEqual(resolved["object"]["version"], 2)
        self.assertIn("url", resolved["download"])
        self.assertGreater(resolved["object"]["expires_at"], "2026-01-01T00:00:00Z")
        self.assertIsNotNone(resolved["object"]["last_accessed_at"])

    def test_upload_file_and_download_file_round_trip(self) -> None:
        source = self.root / "experiments" / "run" / "large.log"
        source.parent.mkdir(parents=True)
        data = b"important run log\n" * 3
        source.write_bytes(data)

        uploaded = self.service.upload_file(
            project_id=self.project_id,
            path=source,
            name="experiments/run/large.log",
            kind="other",
            producing_experiment_id="exp_storage",
        )

        self.assertTrue(uploaded["uploaded"])
        self.assertEqual(uploaded["object"]["status"], "available")
        self.assertEqual(uploaded["object"]["name"], "experiments/run/large.log")
        self.assertEqual(uploaded["object"]["content_sha256"], self._sha(data))
        self.assertEqual(uploaded["object"]["producing_experiment_id"], "exp_storage")

        target = self.root / "downloads" / "large.log"
        downloaded = self.service.download_file(
            project_id=self.project_id,
            object_id=uploaded["object"]["id"],
            path=target,
        )

        self.assertTrue(downloaded["downloaded"])
        self.assertEqual(downloaded["bytes_written"], len(data))
        self.assertEqual(target.read_bytes(), data)

    def test_download_file_refuses_to_clobber_without_overwrite(self) -> None:
        obj = self._put_and_complete(name="datasets/ready.bin", kind="dataset", data=b"new")
        target = self.root / "ready.bin"
        target.write_bytes(b"old")

        with self.assertRaises(ValidationError):
            self.service.download_file(
                project_id=self.project_id,
                object_id=obj["id"],
                path=target,
            )

        self.assertEqual(target.read_bytes(), b"old")
        self.service.download_file(
            project_id=self.project_id,
            object_id=obj["id"],
            path=target,
            overwrite=True,
        )
        self.assertEqual(target.read_bytes(), b"new")

    def test_pin_survives_sweep_and_unpin_renew_restore_expiry(self) -> None:
        obj = self._put_and_complete(name="datasets/pinned.tar", kind="dataset", data=b"pin")

        pinned = self.service.pin(project_id=self.project_id, object_id=obj["id"])
        self.assertIsNone(pinned["expires_at"])
        self.assertEqual(self.service.sweep_expired(now="2999-01-01T00:00:00Z"), 0)
        resolved = self.service.resolve(
            project_id=self.project_id, object_id=obj["id"], include_download=False
        )
        self.assertEqual(resolved["object"]["status"], "available")

        unpinned = self.service.unpin(project_id=self.project_id, object_id=obj["id"])
        self.assertIsNotNone(unpinned["expires_at"])
        renewed = self.service.renew(project_id=self.project_id, object_id=obj["id"])
        self.assertIsNotNone(renewed["expires_at"])

    def test_delete_refcounts_physical_object(self) -> None:
        data = b"shared bytes"
        first = self._put_and_complete(name="datasets/one.tar", kind="dataset", data=data)
        second = self.service.put_object(
            project_id=self.project_id,
            name="datasets/two.tar",
            kind="dataset",
            sha256=self._sha(data),
            size_bytes=len(data),
        )["object"]
        hooked = _HookedObjectStore(self.objects)
        self.objects = hooked
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )

        deleted_first = self.service.delete(project_id=self.project_id, object_id=first["id"])
        self.assertTrue(deleted_first["deleted"])
        self.assertFalse(deleted_first["reclaimed"])
        self.assertEqual(hooked.delete_calls, 0)
        self.assertIsNotNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

        deleted_second = self.service.delete(project_id=self.project_id, object_id=second["id"])
        self.assertTrue(deleted_second["reclaimed"])
        self.assertEqual(hooked.delete_calls, 1)
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

    def test_delete_failure_keeps_last_reference_and_quota_retryable(self) -> None:
        data = b"12345"
        self._set_blob_budget(len(data))
        obj = self._put_and_complete(
            name="datasets/delete-retry.bin", kind="dataset", data=data
        )
        hooked = _HookedObjectStore(self.objects)
        self.objects = hooked
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        hooked.delete_error = RuntimeError("provider delete unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider delete unavailable"):
            self.service.delete(project_id=self.project_id, object_id=obj["id"])

        retained = self.service.get_object(
            project_id=self.project_id, object_id=obj["id"]
        )["object"]
        self.assertEqual(retained["status"], "available")
        self.assertIsNotNone(
            self.objects.stat(namespace=self.project_id, sha256=self._sha(data))
        )
        with self.assertRaises(PermissionDeniedError):
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/blocked-after-delete.bin",
                kind="dataset",
                sha256=self._sha(b"x"),
                size_bytes=1,
            )

        hooked.delete_error = None
        deleted = self.service.delete(project_id=self.project_id, object_id=obj["id"])
        self.assertTrue(deleted["reclaimed"])
        replacement = self.service.put_object(
            project_id=self.project_id,
            name="datasets/replacement-after-delete.bin",
            kind="dataset",
            sha256=self._sha(b"abcde"),
            size_bytes=5,
        )
        self.assertEqual(replacement["object"]["status"], "uploading")

    def test_delete_refuses_upload_reserved_for_completion(self) -> None:
        base = self.objects
        hooked = _HookedObjectStore(base)
        self.objects = hooked
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        data = b"race bytes"
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/race.tar",
            kind="dataset",
            sha256=self._sha(data),
            size_bytes=len(data),
        )
        self._write_upload(registered["upload"], data)
        delete_errors = []

        def delete_reserved() -> None:
            with self.assertRaises(ValidationError) as ctx:
                self.service.delete(
                    project_id=self.project_id,
                    object_id=registered["object"]["id"],
                )
            delete_errors.append(str(ctx.exception))

        hooked.before_complete = delete_reserved

        completed = self.service.complete_upload(
            project_id=self.project_id,
            upload_id=registered["upload"]["upload_id"],
        )

        self.assertEqual(completed["status"], "available")
        self.assertEqual(len(delete_errors), 1)
        self.assertIsNotNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

    def test_complete_reclaims_object_when_reserved_row_is_deleted_before_finish(self) -> None:
        hooked = _HookedObjectStore(self.objects)
        self.objects = hooked
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        data = b"orphan prevention"
        registered = self.service.put_object(
            project_id=self.project_id,
            name="datasets/interleaved.tar",
            kind="dataset",
            sha256=self._sha(data),
            size_bytes=len(data),
        )
        self._write_upload(registered["upload"], data)

        def delete_after_bytes_land() -> None:
            conn = self.store.connect()
            try:
                conn.execute(
                    "UPDATE storage_objects SET status = 'deleted' WHERE id = ?",
                    (registered["object"]["id"],),
                )
                conn.commit()
            finally:
                conn.close()

        hooked.after_complete = delete_after_bytes_land

        with self.assertRaises(NotFoundError):
            self.service.complete_upload(
                project_id=self.project_id,
                upload_id=registered["upload"]["upload_id"],
            )

        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))
        deleted = self.service.list_objects(
            project_id=self.project_id,
            status="deleted",
            include_expired=True,
        )
        self.assertEqual(deleted["total"], 1)

    def test_delete_retry_recovers_when_record_fails_after_provider_delete(self) -> None:
        data = b"durable delete"
        self._set_blob_budget(len(data))
        obj = self._put_and_complete(name="datasets/durable.tar", kind="dataset", data=data)
        original_record = self.service._record

        def fail_delete_record(**kwargs):
            if kwargs.get("event_type") == "storage.deleted":
                raise RuntimeError("record failed")
            return original_record(**kwargs)

        self.service._record = fail_delete_record
        with self.assertRaises(RuntimeError):
            self.service.delete(project_id=self.project_id, object_id=obj["id"])

        still_active = self.service.get_object(
            project_id=self.project_id, object_id=obj["id"]
        )["object"]
        self.assertEqual(still_active["status"], "available")
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))
        with self.assertRaises(PermissionDeniedError):
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/still-reserved.bin",
                kind="dataset",
                sha256=self._sha(b"x"),
                size_bytes=1,
            )

        self.service._record = original_record
        deleted = self.service.delete(project_id=self.project_id, object_id=obj["id"])

        self.assertTrue(deleted["deleted"])
        self.assertFalse(deleted["reclaimed"])
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

    def test_sweep_expired_marks_due_rows_and_refcount_deletes(self) -> None:
        shared = b"shared sweep"
        one = self._put_and_complete(name="datasets/one.tar", kind="dataset", data=shared)
        two = self.service.put_object(
            project_id=self.project_id,
            name="datasets/two.tar",
            kind="dataset",
            sha256=self._sha(shared),
            size_bytes=len(shared),
        )["object"]
        unique = self._put_and_complete(name="datasets/unique.tar", kind="dataset", data=b"unique")
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE storage_objects SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", one["id"]),
            )
            conn.execute(
                "UPDATE storage_objects SET expires_at = ? WHERE id = ?",
                ("2999-01-01T00:00:00Z", two["id"]),
            )
            conn.execute(
                "UPDATE storage_objects SET expires_at = ? WHERE id = ?",
                ("2999-01-01T00:00:00Z", unique["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.service.sweep_expired(now="2026-06-25T00:00:00Z"), 1)
        self.assertIsNotNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(shared)))
        expired = self.service.list_objects(
            project_id=self.project_id, status="expired", include_expired=True
        )
        self.assertEqual(expired["total"], 1)

        conn = self.store.connect()
        try:
            for object_id in (two["id"], unique["id"]):
                conn.execute(
                    "UPDATE storage_objects SET expires_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00Z", object_id),
                )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.service.sweep_expired(now="2026-06-25T00:00:00Z"), 2)
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(shared)))
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(b"unique")))

    def test_sweep_delete_failure_keeps_due_object_retryable(self) -> None:
        data = b"12345"
        self._set_blob_budget(len(data))
        obj = self._put_and_complete(
            name="datasets/sweep-retry.bin", kind="dataset", data=data
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE storage_objects SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", obj["id"]),
            )
        hooked = _HookedObjectStore(self.objects)
        self.objects = hooked
        self.service = StorageLedgerService(
            store=self.store,
            objects=hooked,
            blob_quotas=StorageQuotaService(),
        )
        hooked.delete_error = RuntimeError("provider delete unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider delete unavailable"):
            self.service.sweep_expired(now="2026-06-25T00:00:00Z")

        retained = self.service.get_object(
            project_id=self.project_id, object_id=obj["id"]
        )["object"]
        self.assertEqual(retained["status"], "available")
        with self.assertRaises(PermissionDeniedError):
            self.service.put_object(
                project_id=self.project_id,
                name="datasets/blocked-after-sweep.bin",
                kind="dataset",
                sha256=self._sha(b"x"),
                size_bytes=1,
            )

        hooked.delete_error = None
        self.assertEqual(
            self.service.sweep_expired(now="2026-06-25T00:00:00Z"), 1
        )
        expired = self.service.get_object(
            project_id=self.project_id, object_id=obj["id"]
        )["object"]
        self.assertEqual(expired["status"], "expired")
        self.assertIsNone(
            self.objects.stat(namespace=self.project_id, sha256=self._sha(data))
        )
        replacement = self.service.put_object(
            project_id=self.project_id,
            name="datasets/replacement-after-sweep.bin",
            kind="dataset",
            sha256=self._sha(b"abcde"),
            size_bytes=5,
        )
        self.assertEqual(replacement["object"]["status"], "uploading")

    def _put_and_complete(self, *, name: str, kind: str, data: bytes) -> dict:
        registered = self.service.put_object(
            project_id=self.project_id,
            name=name,
            kind=kind,
            sha256=self._sha(data),
            size_bytes=len(data),
        )
        self._write_upload(registered["upload"], data)
        return self.service.complete_upload(
            project_id=self.project_id, upload_id=registered["upload"]["upload_id"]
        )

    def _write_upload(self, target: dict, data: bytes) -> None:
        url = urlsplit(target["url"])
        self.assertEqual(url.scheme, "file")
        Path(url2pathname(url.path)).write_bytes(data)

    def _sha(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _set_blob_budget(self, size_bytes: int) -> None:
        with self.store.transaction() as conn:
            tenant_id = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (self.project_id,)
            ).fetchone()["tenant_id"]
            conn.execute(
                "INSERT INTO tenant_quotas (tenant_id, blob_bytes_budget) VALUES (?, ?)",
                (tenant_id, size_bytes),
            )


class _HookedObjectStore:
    def __init__(self, inner: FakeObjectStore) -> None:
        self.inner = inner
        self.before_complete = None
        self.after_complete = None
        self.abort_error = None
        self.delete_error = None
        self.delete_calls = 0

    def complete_upload(self, *, upload_id: str, parts: list[dict] | None = None):
        if self.before_complete is not None:
            self.before_complete()
        stat = self.inner.complete_upload(upload_id=upload_id, parts=parts)
        if self.after_complete is not None:
            self.after_complete()
        return stat

    def abort_upload(self, *, upload_id: str) -> bool:
        if self.abort_error is not None:
            raise self.abort_error
        return self.inner.abort_upload(upload_id=upload_id)

    def delete(self, *, namespace: str, sha256: str) -> bool:
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        return self.inner.delete(namespace=namespace, sha256=sha256)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


if __name__ == "__main__":
    unittest.main()
