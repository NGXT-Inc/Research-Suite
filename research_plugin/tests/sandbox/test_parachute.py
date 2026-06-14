"""Expiry parachute + shared transfer spec (cloud plan Phase 5, decision 5).

The transfer contract is one constants module: the rsync flags and the
parachute tar must derive their excludes and size caps from the same values,
so what survives a reaped VM is exactly what a final pull would have brought
home. The flow under test: kill-the-daemon-then-reap (a failing final pull)
provably preserves the experiment dir — parachute over the management
channel, blob-store object recorded on the row, loud events, restore on the
next poll.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.execution.backends.lambda_labs.sandbox_backend import build_user_data
from backend.execution.ssh_rsync import (
    DEFAULT_EXCLUDES as RSYNC_DEFAULT_EXCLUDES,
    SESSIONS_DIR_EXCLUDE as RSYNC_SESSIONS_DIR_EXCLUDE,
)
from backend.execution.transfer_spec import (
    ARTIFACTS_MAX_FILE_SIZE,
    DEFAULT_EXCLUDES,
    PARACHUTE_EXCLUDES,
    SESSIONS_DIR_EXCLUDE,
    SYNC_MAX_FILE_SIZE,
    TRANSFER_CONTRACT_VERSION,
    build_parachute_script,
    is_excluded_relpath,
    max_size_bytes_for,
    max_size_mib,
    parse_parachute_receipt,
    tar_exclude_args,
)
from backend.execution.backends.modal import sandbox_backend as modal_backend_module
from backend.execution import BackendUnavailableError
from backend.services.sync_sessions import (
    TRANSFER_CONTRACT_VERSION as SESSION_TRANSFER_CONTRACT_VERSION,
)
from tests.fakes import FakeRsyncSyncer


class TransferSpecTest(unittest.TestCase):
    def test_rsync_consumes_the_shared_constants(self) -> None:
        # ssh_rsync re-imports the spec's objects — identity, not copies, so
        # the two byte paths can never drift.
        self.assertIs(RSYNC_DEFAULT_EXCLUDES, DEFAULT_EXCLUDES)
        self.assertIs(RSYNC_SESSIONS_DIR_EXCLUDE, SESSIONS_DIR_EXCLUDE)

    def test_session_pin_is_the_spec_version(self) -> None:
        self.assertEqual(SESSION_TRANSFER_CONTRACT_VERSION, TRANSFER_CONTRACT_VERSION)

    def test_tar_excludes_derive_from_the_rsync_excludes(self) -> None:
        # Cross-check (plan Phase 5): every tar --exclude is an rsync exclude
        # pattern (modulo the directory slash), and none is missing.
        rsync_patterns = {p.rstrip("/") for p in PARACHUTE_EXCLUDES}
        tar_patterns = {
            arg.removeprefix("--exclude=") for arg in tar_exclude_args()
        }
        self.assertEqual(tar_patterns, rsync_patterns)
        self.assertEqual(len(tar_exclude_args()), len(PARACHUTE_EXCLUDES))

    def test_size_caps_parse_to_the_documented_mib(self) -> None:
        self.assertEqual(max_size_mib(SYNC_MAX_FILE_SIZE), 100)
        self.assertEqual(max_size_mib(ARTIFACTS_MAX_FILE_SIZE), 5 * 1024)
        self.assertEqual(max_size_bytes_for("results.json"), 100 * 1024 * 1024)
        self.assertEqual(
            max_size_bytes_for("artifacts_to_keep/weights.dat"),
            5 * 1024 * 1024 * 1024,
        )

    def test_python_matcher_mirrors_the_patterns(self) -> None:
        for excluded in (
            ".git/config",
            "src/__pycache__/x.pyc",
            "model.pt",
            "nested/deep/model.pt",
            ".research_plugin_sessions/transcript.log",
            "data.tar.gz",
        ):
            self.assertTrue(is_excluded_relpath(excluded), excluded)
        for kept in ("results.json", "artifacts_to_keep/weights.dat", "train.py"):
            self.assertFalse(is_excluded_relpath(kept), kept)

    def test_parachute_script_tars_the_experiment_dir_with_the_contract(self) -> None:
        script = build_parachute_script()
        self.assertIn('cd "$RP_EXPERIMENT_DIR"', script)
        self.assertIn("--exclude=.git", script)
        self.assertIn("'--exclude=*.pt'", script)
        self.assertIn("--exclude=.research_plugin_sessions", script)
        # The find-based size caps mirror the rsync --max-size pair.
        self.assertIn("-size +100M", script)
        self.assertIn("-size +5120M", script)
        self.assertIn('curl -fsS -T "$TAR" "$URL"', script)
        self.assertIn("sha256sum", script)
        self.assertIn("RP_PARACHUTE sha256=", script)

    def test_parachute_scope_excludes_the_data_dir_and_rp_runs(self) -> None:
        # Pinned invariant (plan Phase 5): $RP_SANDBOX_DATA_DIR/.rp_runs/ env
        # dumps (they can carry HF_TOKEN) are outside the parachute scope —
        # no executable line ever reaches outside $RP_EXPERIMENT_DIR.
        code = "\n".join(
            line
            for line in build_parachute_script().splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn(".rp_runs", code)
        self.assertNotIn("RP_SANDBOX_DATA_DIR", code)
        self.assertNotIn("RP_DATASET_DIR", code)
        self.assertIn('cd "$RP_EXPERIMENT_DIR"', code)

    def test_receipt_parse_round_trip(self) -> None:
        sha = "a" * 64
        receipt = parse_parachute_receipt(
            f"upload noise\nRP_PARACHUTE sha256={sha} size=123\n"
        )
        self.assertEqual(receipt, {"sha256": sha, "size_bytes": 123})
        self.assertIsNone(parse_parachute_receipt("no receipt here"))
        self.assertIsNone(parse_parachute_receipt("RP_PARACHUTE sha256=short size=1"))
        self.assertIsNone(parse_parachute_receipt(f"RP_PARACHUTE sha256={sha} size=x"))

    def test_both_bootstraps_pre_install_the_parachute(self) -> None:
        user_data = build_user_data(
            public_key="ssh-ed25519 AAAAuser k",
            experiment_id="exp1",
            workdir="/workspace/exp1",
            sessions_dir="/workspace/.research_plugin_sessions/exp1",
            sandbox_data_dir="/workspace/data",
            management_public_key="ssh-ed25519 AAAAmgmt k",
        )
        self.assertIn("/opt/rp/parachute.sh", user_data)
        self.assertIn("chmod +x /opt/rp/parachute.sh", user_data)
        # Modal bakes it into the image file layer next to rec.sh.
        modal_source = Path(modal_backend_module.__file__).read_text(encoding="utf-8")
        self.assertIn('"/opt/rp/parachute.sh"', modal_source)
        self.assertIn("build_parachute_script()", modal_source)
        # The fake's captured bootstrap ships the same script.
        self.assertEqual(
            FakeSandboxBackend().bootstrap_files()["/opt/rp/parachute.sh"],
            build_parachute_script(),
        )


def _raise_pull_failure(**_kwargs) -> dict:
    raise BackendUnavailableError("daemon unreachable: injected pull failure")


class ParachuteFlowTest(unittest.TestCase):
    """Reap/release parachute + restore, driven through the app with the fake
    backend (the in-process stand-in for kill-the-daemon-then-reap)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.rsync = FakeRsyncSyncer()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=self.rsync,
        )
        self.project_id = self.call("project.create", name="Parachute Project")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self) -> str:
        exp_id = self.call(
            "experiment.create", name="exp-1", project_id=self.project_id, intent="x"
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,)
            )
        return exp_id

    def _expire(self, exp_id: str) -> None:
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                ("2000-01-01T00:00:00Z", exp_id),
            )

    def _event_types(self) -> list[str]:
        events = self.app.store.recent_events(project_id=self.project_id)["events"]
        return [event["type"] for event in events]

    def _provision_with_remote_files(self, exp_id: str) -> str:
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        sandbox_id = str(created["sandbox_id"])
        self.backend.remote_files[sandbox_id] = {
            "results.json": b'{"accuracy": 0.72}\n',
            "artifacts_to_keep/weights.dat": b"weights",
            "model.pt": b"checkpoint bytes",  # excluded by the contract
            ".git/config": b"[core]\n",  # excluded by the contract
        }
        return sandbox_id

    def test_reap_with_failing_pull_parachutes_then_terminates(self) -> None:
        exp_id = self._experiment()
        sandbox_id = self._provision_with_remote_files(exp_id)
        self._expire(exp_id)
        self.app.sandboxes.daemons._final_pull = _raise_pull_failure

        self.assertEqual(self.app.sandboxes.reap_expired(), 1)

        # The parachute ran over the management channel (management key, not
        # the user key) before the terminate.
        call = self.backend.parachute_calls[-1]
        self.assertEqual(call["sandbox_id"], sandbox_id)
        self.assertEqual(
            Path(call["key_path"]).resolve(),
            (self.repo / ".research_plugin" / "mgmt_keys" / exp_id / "key").resolve(),
        )
        self.assertIn(sandbox_id, self.backend.terminated)
        # The row records the object — key, hash, size, TTL backstop.
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "terminated")
        self.assertEqual(row["parachute_state"], "uploaded")
        self.assertEqual(
            row["parachute_object_key"],
            f"{self.project_id}/{row['parachute_sha256']}",
        )
        self.assertGreater(int(row["parachute_size_bytes"]), 0)
        self.assertTrue(row["parachute_expires_at"])
        # The object is really in the blob store.
        self.assertEqual(
            len(
                self.app.blobs.get(
                    namespace=self.project_id, sha256=row["parachute_sha256"]
                )
            ),
            int(row["parachute_size_bytes"]),
        )
        self.assertIn("sandbox.parachuted", self._event_types())
        self.assertNotIn("sandbox.parachute_failed", self._event_types())

    def test_get_restores_the_unclaimed_parachute(self) -> None:
        exp_id = self._experiment()
        self._provision_with_remote_files(exp_id)
        self._expire(exp_id)
        self.app.sandboxes.daemons._final_pull = _raise_pull_failure
        self.app.sandboxes.reap_expired()

        # The next poll is the reconnect signal: the parachute lands in the
        # experiment folder through the worker, honoring the shared excludes.
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        folder = self.repo / "experiments" / "exp-1"
        self.assertEqual(
            (folder / "results.json").read_bytes(), b'{"accuracy": 0.72}\n'
        )
        self.assertEqual(
            (folder / "artifacts_to_keep" / "weights.dat").read_bytes(), b"weights"
        )
        self.assertFalse((folder / "model.pt").exists())
        self.assertFalse((folder / ".git").exists())
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["parachute_state"], "restored")
        self.assertIn("sandbox.parachute_restored", self._event_types())
        restore_tasks = [
            task
            for task, _ack in self.app.sandboxes.tasks.history
            if task.type == "parachute_restore"
        ]
        self.assertEqual(len(restore_tasks), 1)
        # A second poll does not restore again.
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(
            len(
                [
                    task
                    for task, _ack in self.app.sandboxes.tasks.history
                    if task.type == "parachute_restore"
                ]
            ),
            1,
        )

    def test_parachute_failure_is_loud_and_still_terminates(self) -> None:
        exp_id = self._experiment()
        sandbox_id = self._provision_with_remote_files(exp_id)
        self._expire(exp_id)
        self.app.sandboxes.daemons._final_pull = _raise_pull_failure

        def broken_parachute(**_kwargs):
            raise BackendUnavailableError("VM gone: injected parachute failure")

        self.backend.run_parachute = broken_parachute  # type: ignore[method-assign]
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        self.assertIn(sandbox_id, self.backend.terminated)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "terminated")
        self.assertEqual(row["parachute_state"], "failed")
        self.assertIn("sandbox.parachute_failed", self._event_types())

    def test_release_parachutes_only_when_the_pull_fails(self) -> None:
        # Agent-present release: the pull succeeds, no parachute.
        exp_id = self._experiment()
        self._provision_with_remote_files(exp_id)
        self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(self.backend.parachute_calls, [])

        # Same verb, failing pull: the parachute branch fires (injectable).
        exp2 = self.call(
            "experiment.create", name="exp-2", project_id=self.project_id, intent="x"
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp2,)
            )
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp2
        )
        self.backend.remote_files[str(created["sandbox_id"])] = {
            "results.json": b"{}\n"
        }
        self.app.sandboxes._final_pull_row = _raise_pull_failure  # type: ignore[method-assign]
        released = self.call(
            "sandbox.release", project_id=self.project_id, experiment_id=exp2
        )
        self.assertNotIn("parachute", released["hint"].lower())
        self.assertEqual(len(self.backend.parachute_calls), 1)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp2)
        self.assertEqual(row["parachute_state"], "uploaded")

    def test_restore_of_a_swept_blob_fails_loudly(self) -> None:
        # TTL backstop: once the object is gone, restore marks the row failed
        # and emits the loud event instead of retry-looping forever.
        exp_id = self._experiment()
        self._provision_with_remote_files(exp_id)
        self._expire(exp_id)
        self.app.sandboxes.daemons._final_pull = _raise_pull_failure
        self.app.sandboxes.reap_expired()
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.app.blobs.delete(
            namespace=self.project_id, sha256=row["parachute_sha256"]
        )
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["parachute_state"], "failed")
        self.assertIn("sandbox.parachute_failed", self._event_types())


if __name__ == "__main__":
    unittest.main()
