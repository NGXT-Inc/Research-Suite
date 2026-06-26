"""Experiment names: required at creation, folder-safe, unique per project,
and the source of the experiment folder name (experiments/<name>/)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.utils import ValidationError


class ExperimentNamingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.project_id = self.call("project.create", name="Naming")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _create(self, **kwargs):
        return self.call("experiment.create", project_id=self.project_id, **kwargs)

    def test_name_is_required(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self._create(intent="No name given.")
        self.assertIn("name is required", str(ctx.exception))

    def test_name_must_be_folder_safe(self) -> None:
        for bad in ("ab", "has space", "slash/inside", "trail/", ".hidden", "a" * 49):
            with self.assertRaises(ValidationError, msg=bad):
                self._create(name=bad, intent="Bad name.")

    def test_create_returns_folder_without_eager_local_io(self) -> None:
        exp = self._create(name="lora-rank-sweep", intent="Sweep LoRA ranks.")
        self.assertEqual(exp["name"], "lora-rank-sweep")
        self.assertEqual(exp["folder"], "experiments/lora-rank-sweep/")
        self.assertFalse((self.repo / "experiments" / "lora-rank-sweep").exists())
        self.assertFalse((self.repo / "experiments" / exp["id"]).exists())

    def test_create_response_announces_the_folder(self) -> None:
        exp = self._create(name="lora-rank-sweep", intent="Sweep LoRA ranks.")
        self.assertEqual(exp["folder"], "experiments/lora-rank-sweep/")
        # The directive tells the agent to work inside the folder.
        self.assertIn("experiments/lora-rank-sweep/", exp["folder_guidance"])
        self.assertIn("sandbox", exp["folder_guidance"])

    def test_duplicate_name_is_rejected(self) -> None:
        self._create(name="baseline", intent="First baseline.")
        with self.assertRaises(ValidationError) as ctx:
            self._create(name="baseline", intent="Second baseline.")
        self.assertIn("already exists", str(ctx.exception))
        self.assertIn("baseline", str(ctx.exception))

    def test_duplicate_check_is_case_insensitive(self) -> None:
        self._create(name="Baseline", intent="First.")
        with self.assertRaises(ValidationError) as ctx:
            self._create(name="baseline", intent="Second.")
        self.assertIn("already exists", str(ctx.exception))

    def test_same_name_allowed_across_projects(self) -> None:
        self._create(name="baseline", intent="Project one baseline.")
        other = self.call("project.create", name="Other")["id"]
        exp = self.call(
            "experiment.create",
            project_id=other,
            name="baseline",
            intent="Project two baseline.",
        )
        self.assertEqual(exp["name"], "baseline")

    def test_sandbox_local_experiment_dir_follows_the_name(self) -> None:
        exp = self._create(name="cifar-opt", intent="Optimize CIFAR.")
        local = self.app.worker.local_experiment_dir(
            experiment_id=exp["id"], name=exp["name"]
        )
        # resolve(): tempdirs on macOS live behind the /var -> /private/var symlink.
        self.assertEqual(
            local.resolve(), (self.repo / "experiments" / "cifar-opt").resolve()
        )


if __name__ == "__main__":
    unittest.main()
