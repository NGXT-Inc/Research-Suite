"""ExecutionBackend facade for Modal sandboxes."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from ...errors import BackendUnavailableError, BackendValidationError
from ...types import BackendCapabilities, JobSpec, JobStatus, OutputStatus, ProgressCallback
from ...types import SubmitStatusReport
from .config import ModalConfig, parse_modal_hints
from .runner import (
    RuntimeJobRef,
    build_runtime_job_id,
    cancel_runner,
    decode_runtime_job_id,
)
from .runtime import ModalRuntime
from .submit_pipeline import SubmissionPipeline, _runner_dir_for_job
from .state_reader import ModalStateReader
from .sync import BaselineStore, SyncEngine, SyncPoller


ActivityHook = Callable[[str, dict[str, Any]], None]
ShouldPollProject = Callable[[str], bool]
SANDBOX_IO_TIMEOUT_SECONDS = 5.0


class ModalExecutionBackend:
    def __init__(
        self,
        *,
        repo_root: Path,
        config: ModalConfig | None = None,
        runtime: ModalRuntime | None = None,
        activity: ActivityHook | None = None,
        sync_db_path: Path | None = None,
        sync_engine: SyncEngine | None = None,
        baseline: BaselineStore | None = None,
        poller_interval_seconds: float = 60.0,
        start_poller: bool = True,
        should_poll_project: ShouldPollProject | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or ModalConfig.from_env()
        self.runtime = runtime or ModalRuntime(config=self.config)
        self.activity = activity
        self.capabilities = BackendCapabilities(
            name="modal",
            supports_local_working_dir=False,
            materializes_outputs=True,
        )
        self._sandboxes: dict[str, Any] = {}
        self._volume_objects: dict[str, Any] = {}
        self._post_terminal_synced: set[str] = set()
        # Used for live nested_status while submit is in flight.
        self._live_submits: dict[str, SubmissionPipeline] = {}
        self._live_submits_lock = threading.Lock()

        if sync_engine is not None:
            self.baseline = baseline if baseline is not None else sync_engine.baseline
            self.sync_engine = sync_engine
        else:
            sync_db = sync_db_path or (self.repo_root / ".research_plugin" / "modal" / "sync.sqlite")
            self.baseline = baseline or BaselineStore(db_path=sync_db)
            self.sync_engine = SyncEngine(
                repo_root=self.repo_root,
                baseline=self.baseline,
                volume_provider=self._provide_volume,
                volume_name_prefix=self.config.volume_name_prefix,
                volume_mount_path=self.config.remote_workdir,
                activity=self.activity,
            )
        self.state_reader = ModalStateReader(
            config=self.config,
            sandbox_provider=self._sandbox,
            volume_provider=self._provide_volume,
            retain_sandbox=self._retain,
        )
        self.poller = SyncPoller(
            engine=self.sync_engine,
            baseline=self.baseline,
            interval_seconds=poller_interval_seconds,
            activity=self.activity,
            should_sync_project=should_poll_project,
        )
        if start_poller:
            self.poller.start()

    def on_project_created(self, *, project_id: str) -> None:
        """Register/create the project Volume so the poller can track it."""
        self.sync_engine.ensure_project_volume(project_id=project_id)

    def submit(self, *, spec: JobSpec, progress: ProgressCallback | None = None) -> str:
        """Run one isolated submission pipeline."""
        pipeline = SubmissionPipeline(backend=self)
        job_id = str(spec.metadata.get("research_plugin_job_id") or "")
        if job_id:
            with self._live_submits_lock:
                self._live_submits[job_id] = pipeline
        try:
            return pipeline.run(spec=spec, progress=progress)
        finally:
            if job_id:
                with self._live_submits_lock:
                    self._live_submits.pop(job_id, None)

    def live_submit_status(self, *, job_id: str) -> SubmitStatusReport | None:
        """Return in-flight submit progress, if this process owns it."""
        with self._live_submits_lock:
            pipeline = self._live_submits.get(job_id)
        return pipeline.status_report() if pipeline is not None else None

    def recover_runtime_job_id(
        self,
        *,
        job_id: str,
        project_id: str,
        experiment_id: str,
        backend_hints: dict[str, Any],
    ) -> str | None:
        """Recover a Modal sandbox handle for a submit that blocked after create."""
        hints = parse_modal_hints(backend_hints=backend_hints, config=self.config)
        info = self.sync_engine.ensure_project_volume(project_id=project_id)
        volume_name = info["volume_name"]
        compatibility_key = (
            *hints.compatibility_key,
            volume_name,
            self.config.remote_workdir,
        )
        sandboxes = self.runtime.list_sandboxes(
            tags={
                "research_plugin": "true",
                "research_plugin_job_id": job_id,
                "experiment_id": experiment_id,
                "project_id": project_id,
            }
        )
        sandbox = sandboxes[-1] if sandboxes else None
        if sandbox is None:
            return None
        sandbox_id = str(getattr(sandbox, "object_id", ""))
        if not sandbox_id:
            return None
        runner_dir = _runner_dir_for_job(config=self.config, job_id=job_id)
        runtime_job_id = build_runtime_job_id(
            sandbox_id=sandbox_id,
            job_id=job_id,
            experiment_id=experiment_id,
            project_id=project_id,
            remote_workdir=self.config.remote_workdir,
            runner_dir=runner_dir,
            volume_name=volume_name,
            compatibility_key=compatibility_key,
        )
        self._sandboxes[runtime_job_id] = sandbox
        return runtime_job_id

    def status(self, *, runtime_job_id: str) -> JobStatus:
        return self.state_reader.status(
            runtime_job_id=runtime_job_id,
            timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
        )

    def logs(self, *, runtime_job_id: str) -> str:
        return self.state_reader.logs(
            runtime_job_id=runtime_job_id,
            timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
        )

    def cancel(self, *, runtime_job_id: str) -> bool:
        ref = decode_runtime_job_id(runtime_job_id)
        sandbox = self._sandbox(runtime_job_id=runtime_job_id, ref=ref)
        try:
            return cancel_runner(sandbox=sandbox, config=ref.config_for(self.config))
        finally:
            self._retain(ref=ref, sandbox=sandbox)

    def materialize_outputs(
        self,
        *,
        runtime_job_id: str,
        expected_outputs: Sequence[str],
        repo_root: Path,
    ) -> Sequence[OutputStatus]:
        """Run one post-success sync, then report expected output presence."""
        ref = decode_runtime_job_id(runtime_job_id)
        project_id = ref.project_id or "default"
        if runtime_job_id not in self._post_terminal_synced:
            self.sync_engine.sync(project_id=project_id)
            self._post_terminal_synced.add(runtime_job_id)

        results: list[OutputStatus] = []
        for rel in expected_outputs:
            local = repo_root / rel
            exists = local.exists()
            results.append(
                OutputStatus(
                    path=rel,
                    exists=exists,
                    is_file=local.is_file() if exists else False,
                )
            )
        return results

    def health(self) -> dict:
        return self.runtime.health()

    def shutdown(self) -> None:
        try:
            self.poller.stop()
        except Exception:  # noqa: BLE001
            pass

    def _provide_volume(self, volume_name: str) -> Any:
        volume = self._volume_objects.get(volume_name)
        if volume is None:
            volume = self.runtime.volume_from_name(volume_name)
            self._volume_objects[volume_name] = volume
        # Existing handles need reload() to see fresh commits.
        reload = getattr(volume, "reload", None)
        if callable(reload):
            try:
                reload()
            except Exception:  # noqa: BLE001
                pass
        return volume

    def _reload_sandbox_volumes(self, *, sandbox: Any) -> None:
        reload_volumes = getattr(sandbox, "reload_volumes", None)
        if not callable(reload_volumes):
            return
        try:
            reload_volumes()
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"Modal sandbox volume reload failed: {exc}") from exc

    def _sandbox(self, *, runtime_job_id: str, ref: RuntimeJobRef) -> Any:
        sandbox = self._sandboxes.get(runtime_job_id)
        if sandbox is not None:
            return sandbox
        if not ref.sandbox_id:
            raise BackendValidationError("Modal runtime_job_id is missing sandbox_id")
        sandbox = self.runtime.sandbox_from_id(ref.sandbox_id)
        self._sandboxes[runtime_job_id] = sandbox
        return sandbox

    def _retain(self, *, ref: RuntimeJobRef, sandbox: Any) -> None:
        self.runtime.retain_sandbox(
            sandbox=sandbox,
            experiment_id=ref.experiment_id,
            compatibility_key=ref.compatibility_key,
        )

    def _cleanup_orphaned_sandbox(
        self,
        *,
        sandbox: Any,
        runtime_job_id: str | None = None,
        reason: str = "submit_failed",
    ) -> None:
        """Best-effort cleanup for a sandbox created by a failed submit."""
        sandbox_id = str(getattr(sandbox, "object_id", "") or "")
        if runtime_job_id is not None:
            self._sandboxes.pop(runtime_job_id, None)
        try:
            sandbox.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            detach = getattr(sandbox, "detach", None)
            if callable(detach):
                detach()
        except Exception:  # noqa: BLE001
            pass
        if self.activity is not None:
            try:
                self.activity(
                    "modal.sandbox.terminated_on_submit_failure",
                    {
                        "sandbox_id": sandbox_id,
                        "runtime_job_id": runtime_job_id or "",
                        "reason": reason,
                    },
                )
            except Exception:  # noqa: BLE001
                pass


def build_modal_backend(
    *,
    repo_root: Path,
    activity: ActivityHook | None = None,
    should_poll_project: ShouldPollProject | None = None,
) -> ModalExecutionBackend:
    config = ModalConfig.from_env()
    return ModalExecutionBackend(
        repo_root=repo_root,
        config=config,
        activity=activity,
        should_poll_project=should_poll_project,
    )
