"""Configuration and backend-hint parsing for Modal execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .....kernel.env import env_int, env_raw, env_value
from ....sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT, SESSIONS_DIRNAME


def _env_discovery_disabled() -> bool:
    """True in control mode, where implicit user-machine .env discovery is off.

    Reads MERV_MODE directly (no merv.brain.surface.config import) to keep the
    execution backends loosely coupled from the composition layer. Local mode
    keeps checkout-adjacent .env discovery for development; control resolves
    credentials from the process environment or secret store only.
    """
    return (env_value("MERV_MODE") or "").lower() == "control"


VALID_GPUS: frozenset[str] = frozenset({"T4", "L4", "A10G", "L40S", "A100", "A100-80GB", "H100", "B200"})
DEFAULT_GPU = "A100"

COMPUTE_TIERS: dict[str, dict[str, int]] = {
    "small": {"cpu": 1, "memory": 4096},
    "default": {"cpu": 2, "memory": 8192},
    "large": {"cpu": 4, "memory": 16384},
    "extra_large": {"cpu": 8, "memory": 32768},
}

DEFAULT_APP_NAME = "research-plugin-jobs"
DEFAULT_SANDBOX_DATA_DIR = DEFAULT_DATA_DIR
DEFAULT_RUNNER_DIR = f"{DEFAULT_REMOTE_ROOT}/.merv_job"
DEFAULT_VOLUME_NAME_PREFIX = "research-plugin"
DEFAULT_VOLUME_VERSION = 2
DEFAULT_RETENTION_SECONDS = 600
DEFAULT_SANDBOX_TIMEOUT = 4200
DEFAULT_JOB_TIMEOUT = 3000
# 0 = disabled. Detached runner processes do not keep Modal idle_timeout alive.
DEFAULT_IDLE_TIMEOUT = 0
DEFAULT_TIMEOUT_BUFFER_SECONDS = 60
MAX_MODAL_SANDBOX_TIMEOUT = 24 * 60 * 60

@dataclass(frozen=True)
class ModalConfig:
    app_name: str
    retention_seconds: int
    sandbox_timeout: int
    job_timeout: int
    idle_timeout: int
    # Remote root under which each sandbox work folder (`<root>/<sandbox>`) is created.
    remote_root: str
    sandbox_data_dir: str
    runner_dir: str
    timeout_buffer_seconds: int = DEFAULT_TIMEOUT_BUFFER_SECONDS
    volume_name_prefix: str = DEFAULT_VOLUME_NAME_PREFIX
    volume_version: int = DEFAULT_VOLUME_VERSION

    @classmethod
    def from_env(cls) -> "ModalConfig":
        load_modal_env_file()
        return cls(
            app_name=_env_str("MERV_MODAL_APP", DEFAULT_APP_NAME),
            retention_seconds=_positive_env_int(
                "MERV_MODAL_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS
            ),
            sandbox_timeout=_positive_env_int(
                "MERV_MODAL_SANDBOX_TIMEOUT", DEFAULT_SANDBOX_TIMEOUT
            ),
            job_timeout=_positive_env_int(
                "MERV_MODAL_JOB_TIMEOUT", DEFAULT_JOB_TIMEOUT
            ),
            idle_timeout=_non_negative_env_int(
                "MERV_MODAL_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT
            ),
            remote_root=_absolute_posix_path(
                _env_str("MERV_MODAL_WORKDIR", DEFAULT_REMOTE_ROOT),
                field="MERV_MODAL_WORKDIR",
            ),
            sandbox_data_dir=_absolute_posix_path(
                _env_str("MERV_MODAL_DATA_DIR", DEFAULT_SANDBOX_DATA_DIR),
                field="MERV_MODAL_DATA_DIR",
            ),
            runner_dir=_absolute_posix_path(
                _env_str("MERV_MODAL_RUNNER_DIR", DEFAULT_RUNNER_DIR),
                field="MERV_MODAL_RUNNER_DIR",
            ),
            timeout_buffer_seconds=_positive_env_int(
                "MERV_MODAL_TIMEOUT_BUFFER_SECONDS",
                DEFAULT_TIMEOUT_BUFFER_SECONDS,
            ),
            volume_name_prefix=_env_str(
                "MERV_MODAL_VOLUME_PREFIX",
                DEFAULT_VOLUME_NAME_PREFIX,
            ),
            volume_version=_positive_env_int(
                "MERV_MODAL_VOLUME_VERSION",
                DEFAULT_VOLUME_VERSION,
            ),
        ).validated()

    def validated(self) -> "ModalConfig":
        # The data dir may live under the remote root (e.g. /workspace/data),
        # but must never collide with the locations the plugin manages there:
        # sandbox work folders (`<root>/sandbox-*`) and the sessions tree.
        root = self.remote_root.rstrip("/")
        data = self.sandbox_data_dir.rstrip("/")
        if data == root:
            raise BackendValidationError(
                "MERV_MODAL_DATA_DIR must not equal MERV_MODAL_WORKDIR"
            )
        if _is_under_path(data, root):
            first = data[len(root) + 1 :].split("/", 1)[0]
            if first.startswith("exp_") or first == SESSIONS_DIRNAME:
                raise BackendValidationError(
                    "MERV_MODAL_DATA_DIR must not collide with "
                    f"per-experiment folders or {SESSIONS_DIRNAME} under the remote root"
                )
        return self

    def validate_timeout_budget(self, *, job_timeout: int | None = None) -> None:
        self.sandbox_timeout_for_job(job_timeout=job_timeout or self.job_timeout)

    def sandbox_timeout_for_job(self, *, job_timeout: int) -> int:
        if self.sandbox_timeout > MAX_MODAL_SANDBOX_TIMEOUT:
            raise BackendValidationError(
                f"Modal sandbox timeout must be <= {MAX_MODAL_SANDBOX_TIMEOUT} seconds"
            )
        max_job_timeout = self.max_job_timeout_seconds()
        if job_timeout > max_job_timeout:
            raise BackendValidationError(
                "Modal job timeout exceeds the maximum supported by the sandbox "
                f"lifetime policy: requested {job_timeout}s, max {max_job_timeout}s "
                f"(retention {self.retention_seconds}s + buffer {self.timeout_buffer_seconds}s)"
            )
        required = job_timeout + self.retention_seconds + self.timeout_buffer_seconds
        return max(self.sandbox_timeout, required)

    def max_job_timeout_seconds(self) -> int:
        return max(0, MAX_MODAL_SANDBOX_TIMEOUT - self.retention_seconds - self.timeout_buffer_seconds)


def load_modal_env_file() -> None:
    """Load Modal credentials from an env file without importing dotenv.

    Resolution order:
      1. ``MERV_MODAL_ENV_FILE`` when set (must exist).
      2. A ``.env`` at the merv package root (source-checkout default).

    Values already present in the environment always win over file values, so an
    explicit ``export MODAL_TOKEN_ID=...`` is never overridden.

    Control mode (cloud plan Phase 9, §3.4): user-machine ``.env`` discovery is
    DISABLED — the control plane's provider credentials come from the process
    env / a secret store only, never a checkout's ``.env``. An explicitly
    configured ``MERV_MODAL_ENV_FILE`` is still honored (that IS the
    secret-store seam: point it at a mounted secret file), but the implicit
    package-root ``.env`` fallback is gated off in control mode.
    """

    configured = env_value("MERV_MODAL_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise BackendValidationError(f"MERV_MODAL_ENV_FILE does not exist: {path}")
    elif _env_discovery_disabled():
        return  # control mode: no implicit .env discovery
    else:
        # merv/src/merv/brain/sandbox/execution/backends/modal/config.py -> merv/
        path = Path(__file__).resolve().parents[7] / ".env"
        if not path.exists():
            return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_str(name: str, default: str) -> str:
    raw = env_raw(name)
    value = default.strip() if raw is None else raw
    if not value:
        raise BackendValidationError(f"{name} must not be empty")
    return value


def _positive_env_int(name: str, default: int) -> int:
    parsed = _modal_env_int(name=name, default=default)
    if parsed <= 0:
        raise BackendValidationError(f"{name} must be positive")
    return parsed


def _non_negative_env_int(name: str, default: int) -> int:
    parsed = _modal_env_int(name=name, default=default)
    if parsed < 0:
        raise BackendValidationError(f"{name} must not be negative")
    return parsed


def _modal_env_int(*, name: str, default: int) -> int:
    raw = env_raw(name)
    if raw == "":
        raise BackendValidationError(f"{name} must be an integer")
    try:
        parsed = env_int(name, default)
    except ValueError as exc:
        raise BackendValidationError(f"{name} must be an integer") from exc
    return parsed


def _absolute_posix_path(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise BackendValidationError(f"{field} must be an absolute POSIX path")
    cleaned = path.as_posix().rstrip("/") or "/"
    # A single-segment root like /workspace is fine (it is the default remote
    # root); only genuine system directories are blocked.
    blocked = {
        "/", "/root", "/home", "/usr", "/etc", "/var", "/bin", "/sbin",
        "/lib", "/lib64", "/opt", "/tmp", "/dev", "/proc", "/sys", "/run",
    }
    if cleaned in blocked:
        raise BackendValidationError(f"{field} must not point at a top-level system directory")
    return cleaned


def _is_under_path(child: str, parent: str) -> bool:
    child_path = PurePosixPath(child)
    parent_path = PurePosixPath(parent)
    try:
        child_path.relative_to(parent_path)
    except ValueError:
        return False
    return True
