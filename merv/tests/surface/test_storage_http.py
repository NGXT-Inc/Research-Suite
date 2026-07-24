from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname

from fastapi.testclient import TestClient

from tests.fakes import FakeObjectStore
from tests.support.brain import TestBrain
from merv.brain.surface.composition import build_local_server
from merv.brain.surface.config import STORAGE_PROVIDER_ENV_VAR
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.kernel.state.store import StateStore
from merv.brain.object_storage.service import SINGLE_PUT_MAX_BYTES, StorageLedgerService
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.kernel.utils import ValidationError


def _parse_submit_run(run: str) -> tuple[str, str]:
    """Pull the presigned PUT URL and the completion token out of the compound
    `run` command. Test data carries no single quotes, so a naive scan is safe."""
    put_cmd, complete_cmd = run.split(" && ", 1)
    quoted = re.findall(r"'([^']*)'", put_cmd)
    presigned = quoted[-1]  # curl ... -T '<path>' '<presigned>'
    token = re.search(r"/api/storage/u/([^/]+)/complete", complete_cmd).group(1)
    return presigned, token


class StorageHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        store = StateStore(db_path=self.repo / ".research_plugin" / "state.sqlite")
        storage = StorageLedgerService(store=store, objects=FakeObjectStore())
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            store=store,
            storage=storage,
        )
        self.client = TestClient(create_fastapi_app(self.app.http))
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

    def test_storage_submit_returns_token_curl_command_and_round_trips(self) -> None:
        data = b"tool bytes"
        sha = hashlib.sha256(data).hexdigest()
        submitted = self.app.call_tool(
            "storage.submit",
            {
                "project_id": self.project_id,
                "path": "experiments/storage_demo/run.log",
                "kind": "other",
                "sha256": sha,
                "size_bytes": len(data),
            },
        )
        # Fresh content: an upload is pending, and the compound command is a
        # checksum-bound presigned PUT followed by the completion POST.
        self.assertFalse(submitted["uploaded"])
        self.assertEqual(
            submitted["object"]["name"], "experiments/storage_demo/run.log"
        )
        self.assertEqual(submitted["object"]["status"], "uploading")
        run = submitted["run"]
        import base64

        expected_checksum = base64.b64encode(bytes.fromhex(sha)).decode("ascii")
        self.assertIn(f"-H 'x-amz-checksum-sha256:{expected_checksum}'", run)
        # The presign signs BOTH the checksum and the Content-Type into the
        # SigV4 signature, so the curl must echo the Content-Type header or a
        # real S3/R2 target rejects the PUT with SignatureDoesNotMatch.
        self.assertIn("-H 'Content-Type: ", run)
        self.assertIn("-T 'experiments/storage_demo/run.log'", run)
        self.assertRegex(
            run,
            r"^curl -sf -X PUT .* && curl -sf -X POST "
            r"'http://[^']+/api/storage/u/[^/']+/complete'$",
        )

        # Drive the command: the agent's PUT lands the bytes at the presigned
        # target, then the auth-exempt completion POST finalizes the ledger.
        presigned, token = _parse_submit_run(run)
        Path(url2pathname(urlsplit(presigned).path)).write_bytes(data)
        completed = self.client.post(f"/api/storage/u/{token}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        obj = completed.json()["object"]
        self.assertEqual(obj["status"], "available")
        self.assertEqual(obj["content_sha256"], sha)

        # Single-use: replaying the same token 404s (row deleted on success).
        replay = self.client.post(f"/api/storage/u/{token}/complete")
        self.assertEqual(replay.status_code, 404, replay.text)

    def test_storage_submit_dedup_needs_no_upload(self) -> None:
        data = b"dedup me"
        sha = hashlib.sha256(data).hexdigest()
        first = self._submit_and_complete(
            path="datasets/a.bin", kind="dataset", data=data
        )[0]
        # A second submit of the same name+sha is idempotent: object available,
        # no command, no completion token needed.
        again = self.app.call_tool(
            "storage.submit",
            {
                "project_id": self.project_id,
                "path": "datasets/a.bin",
                "kind": "dataset",
                "sha256": sha,
                "size_bytes": len(data),
            },
        )
        self.assertTrue(again["uploaded"])
        self.assertEqual(again["run"], "")
        self.assertEqual(again["object"]["id"], first["id"])

    def test_storage_submit_enforces_size_caps(self) -> None:
        # Above the 5 GiB single-PUT ceiling -> explicit v1-unsupported error.
        with self.assertRaises(ValidationError) as ctx:
            self.app.call_tool(
                "storage.submit",
                {
                    "project_id": self.project_id,
                    "path": "big.bin",
                    "kind": "other",
                    "sha256": "0" * 64,
                    "size_bytes": SINGLE_PUT_MAX_BYTES + 1,
                },
            )
        self.assertIn("unsupported in v1", str(ctx.exception).lower())

        # An absolute cap (env-configured in composition) rejects before presign.
        capped = StorageLedgerService(
            store=self.app.store, objects=FakeObjectStore(), max_upload_bytes=1024
        )
        with self.assertRaises(ValidationError) as ctx2:
            capped.submit(
                project_id=self.project_id,
                path="over.bin",
                kind="other",
                sha256="0" * 64,
                size_bytes=2048,
            )
        self.assertIn("maximum", str(ctx2.exception).lower())

    def test_storage_completion_token_first_404_before_object_work(self) -> None:
        # An unknown token 404s (token-first) without touching any object.
        resp = self.client.post("/api/storage/u/nonexistent-token/complete")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_storage_fetch_returns_download_and_verify_command(self) -> None:
        data = b"fetch me"
        obj = self._submit_and_complete(
            path="datasets/f.bin", kind="dataset", data=data
        )[0]
        sha = hashlib.sha256(data).hexdigest()
        fetched = self.app.call_tool(
            "storage.fetch",
            {
                "project_id": self.project_id,
                "object_id": obj["id"],
                "path": "local/copy.bin",
            },
        )
        self.assertEqual(fetched["object"]["id"], obj["id"])
        run = fetched["run"]
        # curl the presigned GET to the path, then verify the stored sha256.
        self.assertRegex(run, r"^curl -sf -o 'local/copy\.bin' '[^']+' && ")
        self.assertIn(
            f"printf '%s  %s\\n' {sha} 'local/copy.bin' | shasum -a 256 -c", run
        )

    def test_experiment_state_surfaces_produced_storage_objects(self) -> None:
        exp = self.app.call_tool(
            "experiment.create",
            {
                "project_id": self.project_id,
                "name": "storage-visible",
                "intent": "Retain heavy artifacts in storage.",
            },
        )
        obj, _submitted = self._submit_and_complete(
            path="experiments/storage-visible/model.bin",
            kind="model",
            data=b"model bytes",
            producing_experiment_id=exp["id"],
            producing_run="run-001",
            notes="checkpoint retained for reviewer inspection",
        )
        uploaded = {"object": obj}

        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": self.project_id, "experiment_id": exp["id"]},
        )
        objects = state["storage_objects"]
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["id"], uploaded["object"]["id"])
        self.assertEqual(objects[0]["kind"], "model")
        self.assertEqual(objects[0]["status"], "available")
        self.assertEqual(objects[0]["producing_run"], "run-001")
        self.assertEqual(
            objects[0]["content_sha256"],
            hashlib.sha256(b"model bytes").hexdigest(),
        )
        self.assertNotIn("namespace", objects[0])

        object_id = uploaded["object"]["id"]
        listed = self.app.call_tool(
            "experiment.list", {"project_id": self.project_id}
        )
        self.assertEqual(listed["experiments"][0]["storage_objects"][0]["id"], object_id)
        detail = self._request(
            "GET", f"/api/projects/{self.project_id}/experiments/{exp['id']}"
        )
        self.assertEqual(detail["storage_objects"][0]["id"], object_id)
        filtered = self._request(
            "GET", f"/api/projects/{self.project_id}/experiments?status=planned"
        )
        self.assertEqual(filtered["experiments"][0]["storage_objects"][0]["id"], object_id)
        view = self._request(
            "GET", f"/api/projects/{self.project_id}/experiments/view"
        )
        self.assertEqual(view["experiments"][0]["storage_objects"][0]["id"], object_id)
        status = self._request(
            "GET",
            f"/api/projects/{self.project_id}/status?experiment_id={exp['id']}",
        )
        self.assertEqual(status["experiment"]["storage_objects"][0]["id"], object_id)
        home = self._request("GET", f"/api/projects/{self.project_id}/home")
        self.assertEqual(home["experiments"][0]["storage_objects"][0]["id"], object_id)
        transitioned = self.app.call_tool(
            "experiment.transition",
            {
                "project_id": self.project_id,
                "experiment_id": exp["id"],
                "transition": "abandon",
            },
        )
        self.assertEqual(transitioned["storage_objects"][0]["id"], object_id)

        self.app.call_tool(
            "storage.object",
            {
                "project_id": self.project_id,
                "object_id": uploaded["object"]["id"],
                "action": "delete",
            },
        )
        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": self.project_id, "experiment_id": exp["id"]},
        )
        self.assertEqual(state["storage_objects"], [])

    def test_storage_find_lists_and_resolves_like_the_old_tools(self) -> None:
        a = self._put_and_complete(name="datasets/a.tar", kind="dataset", data=b"aa")
        b = self._put_and_complete(name="models/b.bin", kind="model", data=b"bbbb")

        # List mode (omit selectors) mirrors the former storage.list.
        listed = self.app.call_tool("storage.find", {"project_id": self.project_id})
        self.assertEqual(listed["count"], 2)
        self.assertEqual({o["id"] for o in listed["objects"]}, {a["id"], b["id"]})

        # List mode honours the old filters.
        models = self.app.call_tool(
            "storage.find", {"project_id": self.project_id, "kind": "model"}
        )
        self.assertEqual([o["id"] for o in models["objects"]], [b["id"]])

        # Resolve mode by object_id mirrors the former storage.resolve, with a
        # presigned download and a bumped TTL.
        resolved = self.app.call_tool(
            "storage.find", {"project_id": self.project_id, "object_id": a["id"]}
        )
        self.assertEqual(resolved["object"]["id"], a["id"])
        self.assertIn("url", resolved["download"])

        # Resolve mode by name works too, and include_download=false omits it.
        by_name = self.app.call_tool(
            "storage.find",
            {
                "project_id": self.project_id,
                "name": "models/b.bin",
                "include_download": False,
            },
        )
        self.assertEqual(by_name["object"]["id"], b["id"])
        self.assertNotIn("download", by_name)

    def test_storage_object_dispatches_each_lifecycle_action(self) -> None:
        obj = self._put_and_complete(name="datasets/train.tar", kind="dataset", data=b"data")
        oid = obj["id"]

        # pin/unpin/renew return the bare hydrated object (mirrors the old
        # single-purpose tools).
        pinned = self.app.call_tool(
            "storage.object",
            {"project_id": self.project_id, "object_id": oid, "action": "pin"},
        )
        self.assertIsNone(pinned["expires_at"])

        unpinned = self.app.call_tool(
            "storage.object",
            {"project_id": self.project_id, "object_id": oid, "action": "unpin"},
        )
        self.assertIsNotNone(unpinned["expires_at"])

        renewed = self.app.call_tool(
            "storage.object",
            {"project_id": self.project_id, "object_id": oid, "action": "renew"},
        )
        self.assertIsNotNone(renewed["expires_at"])

        deleted = self.app.call_tool(
            "storage.object",
            {"project_id": self.project_id, "object_id": oid, "action": "delete"},
        )
        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["reclaimed"])

    def test_storage_object_rejects_unknown_action(self) -> None:
        obj = self._put_and_complete(name="datasets/x.tar", kind="dataset", data=b"x")
        with self.assertRaises(ValidationError):
            self.app.call_tool(
                "storage.object",
                {"project_id": self.project_id, "object_id": obj["id"], "action": "purge"},
            )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def _submit_and_complete(
        self, *, path: str, kind: str, data: bytes, **extra: object
    ) -> tuple[dict, dict]:
        """Full token-curl round-trip: storage.submit -> presigned PUT (to the
        fake target) -> auth-exempt completion POST. Returns (object, submit)."""
        sha = hashlib.sha256(data).hexdigest()
        submitted = self.app.call_tool(
            "storage.submit",
            {
                "project_id": self.project_id,
                "path": path,
                "kind": kind,
                "sha256": sha,
                "size_bytes": len(data),
                **extra,
            },
        )
        presigned, token = _parse_submit_run(submitted["run"])
        Path(url2pathname(urlsplit(presigned).path)).write_bytes(data)
        completed = self.client.post(f"/api/storage/u/{token}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        return completed.json()["object"], submitted

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
                server = build_local_server(state_dir=root)
                app = server.app
            try:
                self.assertIsNone(app._storage)
                self.assertFalse(
                    {tool["name"] for tool in app.tools.list_tools()}
                    & {"storage.put_object", "storage.find", "storage.object"}
                )
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
