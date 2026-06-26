from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.sandbox.sandbox_daemons import SandboxDaemons
from backend.services.sandbox.sandbox_heartbeat import SandboxIdlePolicy
from backend.utils import format_iso


def _sample(
    *,
    cpu: float = 0.0,
    gpu: int = 0,
    mem: int = 1_000_000,
    net: int = 1_000,
    ssh: int = 0,
) -> dict:
    return {
        "cpu": {"used_cores": cpu, "limit_cores": 2.0},
        "memory": {"used_bytes": mem, "limit_bytes": None},
        "network": {"bytes_total": net, "ssh_established": ssh},
        "gpus": [{"index": 0, "util_pct": gpu}],
    }


class SandboxIdlePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = SandboxIdlePolicy()
        self.previous = _sample()

    def test_all_quiet_is_idle(self) -> None:
        self.assertTrue(
            self.policy.is_idle(
                current=_sample(),
                previous=self.previous,
                elapsed_seconds=30,
            )
        )

    def test_unmeasurable_ssh_does_not_block_idle(self) -> None:
        # ss/proc absent (e.g. Modal has no sshd) → ssh_established is None;
        # that must not make an otherwise-quiet box un-reapable.
        self.assertTrue(
            self.policy.is_idle(
                current=_sample(ssh=None),
                previous=self.previous,
                elapsed_seconds=30,
            )
        )

    def test_any_activity_signal_is_not_idle(self) -> None:
        cases = {
            "network": _sample(net=100_000),
            "cpu": _sample(cpu=0.25),
            "gpu": _sample(gpu=20),
            "ram": _sample(mem=100_000_000),
            "ssh": _sample(ssh=1),
        }
        for name, current in cases.items():
            with self.subTest(signal=name):
                self.assertFalse(
                    self.policy.is_idle(
                        current=current,
                        previous=self.previous,
                        elapsed_seconds=30,
                    )
                )

    def test_idle_window_accumulates_to_reap_threshold(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        started = self.policy.next_idle_since(idle_since=None, now=now, is_idle=True)
        self.assertEqual(started, now)
        self.assertFalse(
            self.policy.should_reap(
                idle_since=now - timedelta(seconds=3599),
                now=now,
                threshold_seconds=3600,
            )
        )
        self.assertTrue(
            self.policy.should_reap(
                idle_since=now - timedelta(seconds=3600),
                now=now,
                threshold_seconds=3600,
            )
        )

    def test_activity_resets_idle_window(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertIsNone(
            self.policy.next_idle_since(
                idle_since=now - timedelta(hours=2),
                now=now,
                is_idle=False,
            )
        )


class SandboxHeartbeatMonitorTest(unittest.TestCase):
    _ENV = {"RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._saved = {key: os.environ.get(key) for key in self._ENV}
        os.environ.update(self._ENV)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.app.call_tool(
            "project.create", {"name": "Heartbeat Project"}
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _experiment(self, name: str) -> str:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": name, "intent": "x"},
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,)
            )
        return exp_id

    def _request(self, exp_id: str) -> dict:
        return self.app.call_tool(
            "sandbox.request",
            {"project_id": self.project_id, "experiment_id": exp_id},
        )

    def _seed_heartbeat(
        self,
        *,
        exp_id: str,
        sandbox_uid: str,
        sampled_at: datetime,
        idle_since: datetime,
        metrics: dict,
    ) -> None:
        self.app.sandboxes.registry.record_heartbeat(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            idle_since=format_iso(idle_since),
            snapshot={"sampled_at": format_iso(sampled_at), "metrics": metrics},
        )

    def test_idle_sandbox_is_reaped_while_busy_sandbox_is_spared(self) -> None:
        now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        previous_at = now - timedelta(seconds=30)
        idle_since = now - timedelta(hours=1)
        idle_exp = self._experiment("idle")
        busy_exp = self._experiment("busy")
        idle = self._request(idle_exp)
        busy = self._request(busy_exp)
        for exp_id, sandbox_uid in (
            (idle_exp, idle["sandbox_uid"]),
            (busy_exp, busy["sandbox_uid"]),
        ):
            self._seed_heartbeat(
                exp_id=exp_id,
                sandbox_uid=str(sandbox_uid),
                sampled_at=previous_at,
                idle_since=idle_since,
                metrics=_sample(),
            )
        self.backend.metrics[idle["sandbox_id"]] = _sample()
        self.backend.metrics[busy["sandbox_id"]] = _sample(cpu=0.5)

        reaped = self.app.sandboxes.reap_idle(now=now, threshold_seconds=3600)

        self.assertEqual(reaped, 1)
        self.assertIn(idle["sandbox_id"], self.backend.terminated)
        self.assertNotIn(busy["sandbox_id"], self.backend.terminated)
        self.assertEqual(
            self.app.sandboxes.get(
                project_id=self.project_id,
                experiment_id=idle_exp,
                sandbox_uid=str(idle["sandbox_uid"]),
            )["status"],
            "terminated",
        )
        self.assertEqual(
            self.app.sandboxes.get(project_id=self.project_id, experiment_id=busy_exp)[
                "status"
            ],
            "running",
        )
        events = self.app.store.recent_events(project_id=self.project_id)["events"]
        idle_events = [
            event
            for event in events
            if event["type"] == "sandbox.idle_reaped" and event["target_id"] == idle_exp
        ]
        self.assertEqual(len(idle_events), 1)
        self.assertEqual(idle_events[0]["payload"]["idle_seconds"], 3600)

    def test_zero_threshold_disables_idle_reaping(self) -> None:
        exp_id = self._experiment("disabled")
        created = self._request(exp_id)
        now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        self._seed_heartbeat(
            exp_id=exp_id,
            sandbox_uid=str(created["sandbox_uid"]),
            sampled_at=now - timedelta(seconds=30),
            idle_since=now - timedelta(hours=2),
            metrics=_sample(),
        )
        self.backend.metrics[created["sandbox_id"]] = _sample()

        self.assertEqual(
            self.app.sandboxes.reap_idle(now=now, threshold_seconds=0),
            0,
        )
        self.assertNotIn(created["sandbox_id"], self.backend.terminated)


class SandboxHeartbeatEnvTest(unittest.TestCase):
    def _daemons(self) -> SandboxDaemons:
        return SandboxDaemons(
            registry=object(),  # type: ignore[arg-type]
            backend=FakeSandboxBackend(),
            provisioner=object(),  # type: ignore[arg-type]
            experiments=object(),  # type: ignore[arg-type]
            persist_metrics=lambda **_kwargs: None,
        )

    def test_idle_threshold_zero_or_empty_disables_idle_reaping(self) -> None:
        daemons = self._daemons()
        for value in ("0", ""):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"RESEARCH_PLUGIN_SANDBOX_IDLE_SECONDS": value},
                    clear=False,
                ):
                    self.assertEqual(daemons._idle_reap_threshold(), 0)


if __name__ == "__main__":
    unittest.main()
