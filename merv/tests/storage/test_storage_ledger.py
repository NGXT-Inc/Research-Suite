from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from tests.fakes import FakeObjectStore
from backend.state.store import StateStore
from backend.storage.service import STORAGE_DEFAULT_TTL_SECONDS, StorageLedgerService
from backend.utils import NotFoundError, ValidationError, parse_iso


class StorageLedgerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StateStore(db_path=self.root / "state.sqlite")
        self.objects = FakeObjectStore()
        self.service = StorageLedgerService(store=self.store, objects=self.objects)
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
        self.assertIsNone(registered["object"]["expires_at"])
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

        deleted_first = self.service.delete(project_id=self.project_id, object_id=first["id"])
        self.assertTrue(deleted_first["deleted"])
        self.assertFalse(deleted_first["reclaimed"])
        self.assertIsNotNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

        deleted_second = self.service.delete(project_id=self.project_id, object_id=second["id"])
        self.assertTrue(deleted_second["reclaimed"])
        self.assertIsNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

    def test_delete_refuses_upload_reserved_for_completion(self) -> None:
        base = self.objects
        hooked = _HookedObjectStore(base)
        self.objects = hooked
        self.service = StorageLedgerService(store=self.store, objects=hooked)
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
        self.service = StorageLedgerService(store=self.store, objects=hooked)
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

    def test_reclaim_waits_for_durable_delete_record(self) -> None:
        data = b"durable delete"
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
        self.assertIsNotNone(self.objects.stat(namespace=self.project_id, sha256=self._sha(data)))

        self.service._record = original_record
        deleted = self.service.delete(project_id=self.project_id, object_id=obj["id"])

        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["reclaimed"])
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


class _HookedObjectStore:
    def __init__(self, inner: FakeObjectStore) -> None:
        self.inner = inner
        self.before_complete = None
        self.after_complete = None

    def complete_upload(self, *, upload_id: str, parts: list[dict] | None = None):
        if self.before_complete is not None:
            self.before_complete()
        stat = self.inner.complete_upload(upload_id=upload_id, parts=parts)
        if self.after_complete is not None:
            self.after_complete()
        return stat

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


if __name__ == "__main__":
    unittest.main()
