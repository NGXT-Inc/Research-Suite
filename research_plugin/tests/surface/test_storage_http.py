from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname

from fastapi.testclient import TestClient

from tests.fakes import FakeObjectStore
from backend.app import ResearchPluginApp
from backend.composition.local_mode import build_local_app
from backend.config import STORAGE_PROVIDER_ENV_VAR
from backend.execution.backends.fake import FakeSandboxBackend
from backend.state.store import StateStore
from backend.storage.service import StorageLedgerService
from backend.transport.http_api import create_fastapi_app


class StorageHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        store = StateStore(db_path=self.repo / ".research_plugin" / "state.sqlite")
        storage = StorageLedgerService(store=store, objects=FakeObjectStore())
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            store=store,
            storage=storage,
        )
        self.client = TestClient(create_fastapi_app(self.app))
        self.project_id = self._request(
            "POST", "/api/projects", {"name": "Storage HTTP Project"}
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_storage_routes_list_get_download_pin_renew_delete(self) -> None:
        obj = self._put_and_complete(name="datasets/train.tar", kind="dataset", data=b"data")
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE storage_objects SET last_accessed_at = ?, expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", "2026-01-01T00:00:00Z", obj["id"]),
            )

        listed = self._request("GET", f"/api/projects/{self.project_id}/storage")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["objects"][0]["id"], obj["id"])

        got = self._request("GET", f"/api/projects/{self.project_id}/storage/{obj['id']}")
        self.assertEqual(got["object"]["last_accessed_at"], "2000-01-01T00:00:00Z")
        self.assertNotIn("download", got)

        downloaded = self._request(
            "POST", f"/api/projects/{self.project_id}/storage/{obj['id']}/download"
        )
        self.assertIn("url", downloaded["download"])
        self.assertGreater(downloaded["object"]["expires_at"], "2026-01-01T00:00:00Z")

        pinned = self._request("POST", f"/api/projects/{self.project_id}/storage/{obj['id']}/pin")
        self.assertIsNone(pinned["object"]["expires_at"])
        renewed = self._request("POST", f"/api/projects/{self.project_id}/storage/{obj['id']}/renew")
        self.assertIsNotNone(renewed["object"]["expires_at"])

        deleted = self._request("DELETE", f"/api/projects/{self.project_id}/storage/{obj['id']}")
        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["reclaimed"])
        self.assertEqual(
            self._request("GET", f"/api/projects/{self.project_id}/storage")["objects"],
            [],
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def _put_and_complete(self, *, name: str, kind: str, data: bytes) -> dict:
        registered = self.app.storage.put_object(
            project_id=self.project_id,
            name=name,
            kind=kind,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        target = urlsplit(registered["upload"]["url"])
        self.assertEqual(target.scheme, "file")
        Path(url2pathname(target.path)).write_bytes(data)
        return self.app.storage.complete_upload(
            project_id=self.project_id,
            upload_id=registered["upload"]["upload_id"],
        )


class StorageCompositionTest(unittest.TestCase):
    def test_local_mode_disables_storage_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "RESEARCH_PLUGIN_EXECUTION_BACKEND": "fake",
                    STORAGE_PROVIDER_ENV_VAR: "",
                },
            ):
                app = build_local_app(
                    repo_root=root,
                    db_path=root / ".research_plugin" / "state.sqlite",
                )
            try:
                self.assertIsNone(app.storage)
                self.assertFalse(
                    {tool["name"] for tool in app.list_tools()}
                    & {"storage.put_object", "storage.list"}
                )
            finally:
                app.shutdown()


if __name__ == "__main__":
    unittest.main()
