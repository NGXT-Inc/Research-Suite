"""Modal app, image, sandbox lifecycle, and retention management."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from ...errors import BackendUnavailableError
from .config import ModalConfig, ModalJobHints


@dataclass
class RetainedSandbox:
    sandbox_id: str
    sandbox: Any
    experiment_id: str
    compatibility_key: tuple[Any, ...]
    expires_at: float


class ModalRuntime:
    """Small Modal runtime wrapper isolated from the backend contract."""

    def __init__(self, *, config: ModalConfig, modal_module: Any | None = None) -> None:
        self.config = config
        self._modal = modal_module
        self._app = None
        self._base_image = None
        self._cuda_devel_image = None
        self._lock = threading.Lock()
        self._retained: dict[str, RetainedSandbox] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._swept_expired = False

    def get_or_create_sandbox(
        self,
        *,
        hints: ModalJobHints,
        metadata: Mapping[str, str],
        volumes: Mapping[str, Any] | None = None,
        compatibility_key: tuple[Any, ...] | None = None,
    ) -> Any:
        experiment_id = metadata.get("experiment_id", "")
        reuse_key = compatibility_key or hints.compatibility_key
        tags = _job_tags(metadata=metadata, experiment_id=experiment_id)
        retained = self._find_retained(experiment_id=experiment_id, compatibility_key=reuse_key)
        if retained is not None:
            self._set_tags(sandbox=retained.sandbox, tags=tags)
            return retained.sandbox

        self._ensure_credentials()
        self._sweep_expired_retained_once()
        modal = self._modal_module()
        image = self._image(hints=hints)
        app = self._get_app()
        kwargs: dict[str, Any] = {
            "app": app,
            "image": image,
            "name": _sandbox_name(metadata),
            "gpu": hints.gpu,
            "cpu": hints.cpu,
            "memory": hints.memory,
            "timeout": self.config.sandbox_timeout_for_job(job_timeout=hints.timeout),
            "workdir": "/workspace",
        }
        # Detached runners do not count as Modal activity, so idle timeout is opt-in.
        if self.config.idle_timeout and self.config.idle_timeout > 0:
            kwargs["idle_timeout"] = self.config.idle_timeout
        if volumes:
            kwargs["volumes"] = dict(volumes)
        if hints.cloud:
            kwargs["cloud"] = hints.cloud
        if hints.region:
            kwargs["region"] = hints.region
        try:
            return self._create_sandbox(modal=modal, kwargs=kwargs, tags=tags)
        except Exception:
            with self._lock:
                self._app = None
            try:
                kwargs["app"] = self._get_app()
                return self._create_sandbox(modal=modal, kwargs=kwargs, tags=tags)
            except Exception as exc:
                raise BackendUnavailableError(f"Modal sandbox create failed: {exc}") from exc

    def sandbox_from_id(self, sandbox_id: str) -> Any:
        modal = self._modal_module()
        try:
            return modal.Sandbox.from_id(sandbox_id)
        except Exception as exc:
            raise BackendUnavailableError(f"Modal sandbox is unavailable: {sandbox_id}: {exc}") from exc

    def list_sandboxes(self, *, tags: Mapping[str, str] | None = None) -> list[Any]:
        self._ensure_credentials()
        modal = self._modal_module()
        app = self._get_app()
        app_id = getattr(app, "app_id", None)
        try:
            return _collect_sandboxes(
                modal.Sandbox.list(
                    app_id=app_id,
                    tags=dict(tags or {}),
                )
            )
        except Exception as exc:
            raise BackendUnavailableError(f"Modal sandbox listing failed: {exc}") from exc

    def volume_from_name(self, volume_name: str) -> Any:
        self._ensure_credentials()
        modal = self._modal_module()
        try:
            return modal.Volume.from_name(volume_name, create_if_missing=True)
        except Exception as exc:
            raise BackendUnavailableError(f"Modal volume is unavailable: {volume_name}: {exc}") from exc

    def retain_sandbox(
        self,
        *,
        sandbox: Any,
        experiment_id: str,
        compatibility_key: tuple[Any, ...],
        delay_seconds: int | None = None,
    ) -> None:
        sandbox_id = str(getattr(sandbox, "object_id", ""))
        if not sandbox_id:
            return
        delay = delay_seconds if delay_seconds is not None else self.config.retention_seconds
        expires_at = time.time() + delay
        with self._lock:
            self._retained[sandbox_id] = RetainedSandbox(
                sandbox_id=sandbox_id,
                sandbox=sandbox,
                experiment_id=experiment_id,
                compatibility_key=compatibility_key,
                expires_at=expires_at,
            )
            old_timer = self._timers.pop(sandbox_id, None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(delay, self.terminate_sandbox, kwargs={"sandbox_id": sandbox_id})
            timer.daemon = True
            self._timers[sandbox_id] = timer
            timer.start()
        self._set_retention_tag(sandbox=sandbox, expires_at=expires_at)

    def terminate_sandbox(self, *, sandbox_id: str) -> None:
        with self._lock:
            retained = self._retained.pop(sandbox_id, None)
            timer = self._timers.pop(sandbox_id, None)
            if timer is not None:
                timer.cancel()
        sandbox = retained.sandbox if retained is not None else None
        if sandbox is None:
            try:
                sandbox = self.sandbox_from_id(sandbox_id)
            except BackendUnavailableError:
                return
        try:
            sandbox.terminate()
        except Exception:
            pass
        try:
            sandbox.detach()
        except Exception:
            pass

    def health(self) -> dict[str, Any]:
        try:
            self._ensure_credentials()
            self._get_app()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": "modal", "error": str(exc)}
        return {"ok": True, "name": "modal", "app": self.config.app_name}

    def sweep_expired_retained_sandboxes(self, *, now: float | None = None) -> int:
        sandboxes = self.list_sandboxes(tags={"research_plugin": "true"})
        terminated = 0
        current_time = time.time() if now is None else now
        for sandbox in sandboxes:
            retained_until = _retained_until(sandbox)
            if retained_until is None or retained_until > current_time:
                continue
            try:
                sandbox.terminate()
                terminated += 1
            except Exception:
                pass
            try:
                sandbox.detach()
            except Exception:
                pass
        return terminated

    def _find_retained(
        self,
        *,
        experiment_id: str,
        compatibility_key: tuple[Any, ...],
    ) -> RetainedSandbox | None:
        now = time.time()
        with self._lock:
            retained = [
                item
                for item in self._retained.values()
                if item.experiment_id == experiment_id
                and item.compatibility_key == compatibility_key
                and item.expires_at > now
            ]
        for item in retained[:1]:
            if self._sandbox_alive(item.sandbox):
                return item
            with self._lock:
                self._retained.pop(item.sandbox_id, None)
        return None

    def _sandbox_alive(self, sandbox: Any) -> bool:
        try:
            process = sandbox.exec("echo", "ok", timeout=10)
            wait = getattr(process, "wait", None)
            return int(wait() if callable(wait) else 0) == 0
        except Exception:
            return False

    def _sweep_expired_retained_once(self) -> None:
        with self._lock:
            if self._swept_expired:
                return
            self._swept_expired = True
        try:
            self.sweep_expired_retained_sandboxes()
        except Exception:
            with self._lock:
                self._swept_expired = False

    def _create_sandbox(
        self,
        *,
        modal: Any,
        kwargs: dict[str, Any],
        tags: dict[str, str],
    ) -> Any:
        sandbox = modal.Sandbox.create(**kwargs)
        self._set_tags(sandbox=sandbox, tags=tags)
        return sandbox

    def _get_app(self) -> Any:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = self._modal_module().App.lookup(
                        self.config.app_name,
                        create_if_missing=True,
                    )
        return self._app

    def _image(self, *, hints: ModalJobHints) -> Any:
        base = self._base_cuda_image() if hints.cuda_devel else self._base_image_default()
        if hints.image_packages:
            return base.pip_install(*hints.image_packages)
        return base

    def _base_image_default(self) -> Any:
        if self._base_image is None:
            with self._lock:
                if self._base_image is None:
                    modal = self._modal_module()
                    self._base_image = (
                        modal.Image.debian_slim(python_version="3.11")
                        .apt_install("ca-certificates", "curl")
                        .pip_install("uv")
                        .run_commands(
                            "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                            "uv pip install --system transformers numpy matplotlib pandas scikit-learn modal",
                        )
                    )
        return self._base_image

    def _base_cuda_image(self) -> Any:
        if self._cuda_devel_image is None:
            with self._lock:
                if self._cuda_devel_image is None:
                    modal = self._modal_module()
                    self._cuda_devel_image = (
                        modal.Image.from_registry(
                            "nvidia/cuda:12.1.1-devel-ubuntu22.04",
                            add_python="3.11",
                        )
                        .apt_install("ca-certificates", "curl")
                        .pip_install("uv")
                        .run_commands(
                            "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                            "uv pip install --system transformers numpy matplotlib pandas scikit-learn ninja modal",
                        )
                    )
        return self._cuda_devel_image

    def _modal_module(self) -> Any:
        if self._modal is None:
            try:
                import modal  # type: ignore
            except ImportError as exc:
                raise BackendUnavailableError("modal SDK is not installed") from exc
            self._modal = modal
        return self._modal

    def _ensure_credentials(self) -> None:
        if not os.environ.get("MODAL_TOKEN_ID") or not os.environ.get("MODAL_TOKEN_SECRET"):
            raise BackendUnavailableError(
                "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET are required for Modal execution"
            )

    def _set_retention_tag(self, *, sandbox: Any, expires_at: float) -> None:
        self._set_tags(
            sandbox=sandbox,
            tags={"research_plugin_retained_until": str(int(expires_at))},
        )

    def _set_tags(self, *, sandbox: Any, tags: Mapping[str, str]) -> None:
        set_tags = getattr(sandbox, "set_tags", None)
        if not callable(set_tags):
            return
        try:
            existing = {}
            get_tags = getattr(sandbox, "get_tags", None)
            if callable(get_tags):
                existing = dict(get_tags())
            existing.update(tags)
            set_tags(existing)
        except Exception:
            pass


def _job_tags(*, metadata: Mapping[str, str], experiment_id: str) -> dict[str, str]:
    return {
        "research_plugin": "true",
        "research_plugin_job_id": metadata.get("research_plugin_job_id", ""),
        "experiment_id": experiment_id,
        "project_id": metadata.get("project_id", ""),
    }


def _sandbox_name(metadata: Mapping[str, str]) -> str | None:
    job_id = metadata.get("research_plugin_job_id")
    if not job_id:
        return None
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-")
    return f"rp-{safe_job_id or 'job'}"[:63]


def _retained_until(sandbox: Any) -> float | None:
    get_tags = getattr(sandbox, "get_tags", None)
    if not callable(get_tags):
        return None
    try:
        raw = get_tags().get("research_plugin_retained_until")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _collect_sandboxes(value: Any) -> list[Any]:
    if hasattr(value, "__aiter__"):
        async def collect() -> list[Any]:
            return [item async for item in value]

        return asyncio.run(collect())
    return list(value)
