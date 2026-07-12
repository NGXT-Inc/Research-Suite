"""Configuration and backend-hint parsing for Modal execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ....env import env_int
from ....sandbox.sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT, SESSIONS_DIRNAME


def _env_discovery_disabled() -> bool:
    """True in control mode, where implicit user-machine .env discovery is off.

    Reads RESEARCH_PLUGIN_MODE directly (no backend.config import) to keep the
    execution backends loosely coupled from the composition layer. Local mode
    keeps checkout-adjacent .env discovery for development; control resolves
    credentials from the process environment or secret store only.
    """
    return (os.environ.get("RESEARCH_PLUGIN_MODE") or "").strip().lower() == "control"


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
DEFAULT_RUNNER_DIR = f"{DEFAULT_REMOTE_ROOT}/.research_plugin_job"
DEFAULT_VOLUME_NAME_PREFIX = "research-plugin"
DEFAULT_VOLUME_VERSION = 2
DEFAULT_RETENTION_SECONDS = 600
DEFAULT_SANDBOX_TIMEOUT = 4200
DEFAULT_JOB_TIMEOUT = 3000
# 0 = disabled. Detached runner processes do not keep Modal idle_timeout alive.
DEFAULT_IDLE_TIMEOUT = 0
DEFAULT_TIMEOUT_BUFFER_SECONDS = 60
MAX_MODAL_SANDBOX_TIMEOUT = 24 * 60 * 60

_KNOWN_HINTS = frozenset(
    {
        "cloud",
        "compute_tier",
        "cuda_devel",
        "experiment_path",
        "gpu",
        "image_packages",
        "notes",
        "region",
        "timeout",
    }
)


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
            app_name=_env_str("RESEARCH_PLUGIN_MODAL_APP", DEFAULT_APP_NAME),
            retention_seconds=_positive_env_int(
                "RESEARCH_PLUGIN_MODAL_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS
            ),
            sandbox_timeout=_positive_env_int(
                "RESEARCH_PLUGIN_MODAL_SANDBOX_TIMEOUT", DEFAULT_SANDBOX_TIMEOUT
            ),
            job_timeout=_positive_env_int(
                "RESEARCH_PLUGIN_MODAL_JOB_TIMEOUT", DEFAULT_JOB_TIMEOUT
            ),
            idle_timeout=_non_negative_env_int(
                "RESEARCH_PLUGIN_MODAL_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT
            ),
            remote_root=_absolute_posix_path(
                _env_str("RESEARCH_PLUGIN_MODAL_WORKDIR", DEFAULT_REMOTE_ROOT),
                field="RESEARCH_PLUGIN_MODAL_WORKDIR",
            ),
            sandbox_data_dir=_absolute_posix_path(
                _env_str("RESEARCH_PLUGIN_MODAL_DATA_DIR", DEFAULT_SANDBOX_DATA_DIR),
                field="RESEARCH_PLUGIN_MODAL_DATA_DIR",
            ),
            runner_dir=_absolute_posix_path(
                _env_str("RESEARCH_PLUGIN_MODAL_RUNNER_DIR", DEFAULT_RUNNER_DIR),
                field="RESEARCH_PLUGIN_MODAL_RUNNER_DIR",
            ),
            timeout_buffer_seconds=_positive_env_int(
                "RESEARCH_PLUGIN_MODAL_TIMEOUT_BUFFER_SECONDS",
                DEFAULT_TIMEOUT_BUFFER_SECONDS,
            ),
            volume_name_prefix=_env_str(
                "RESEARCH_PLUGIN_MODAL_VOLUME_PREFIX",
                DEFAULT_VOLUME_NAME_PREFIX,
            ),
            volume_version=_positive_env_int(
                "RESEARCH_PLUGIN_MODAL_VOLUME_VERSION",
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
                "RESEARCH_PLUGIN_MODAL_DATA_DIR must not equal RESEARCH_PLUGIN_MODAL_WORKDIR"
            )
        if _is_under_path(data, root):
            first = data[len(root) + 1 :].split("/", 1)[0]
            if first.startswith("exp_") or first == SESSIONS_DIRNAME:
                raise BackendValidationError(
                    "RESEARCH_PLUGIN_MODAL_DATA_DIR must not collide with "
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


@dataclass(frozen=True)
class ModalJobHints:
    gpu: str
    compute_tier: str
    cuda_devel: bool
    image_packages: tuple[str, ...]
    timeout: int
    experiment_path: str | None
    cloud: str | None
    region: str | tuple[str, ...] | None

    @property
    def cpu(self) -> int:
        return COMPUTE_TIERS[self.compute_tier]["cpu"]

    @property
    def memory(self) -> int:
        return COMPUTE_TIERS[self.compute_tier]["memory"]

    @property
    def compatibility_key(self) -> tuple[Any, ...]:
        return (
            self.gpu,
            self.compute_tier,
            self.cuda_devel,
            self.image_packages,
            self.timeout,
            self.cloud,
            self.region,
        )


def load_modal_env_file() -> None:
    """Load Modal credentials from an env file without importing dotenv.

    Resolution order:
      1. ``RESEARCH_PLUGIN_MODAL_ENV_FILE`` when set (must exist).
      2. A ``.env`` at the merv package root (source-checkout default).

    Values already present in the environment always win over file values, so an
    explicit ``export MODAL_TOKEN_ID=...`` is never overridden.

    Control mode (cloud plan Phase 9, §3.4): user-machine ``.env`` discovery is
    DISABLED — the control plane's provider credentials come from the process
    env / a secret store only, never a checkout's ``.env``. An explicitly
    configured ``RESEARCH_PLUGIN_MODAL_ENV_FILE`` is still honored (that IS the
    secret-store seam: point it at a mounted secret file), but the implicit
    package-root ``.env`` fallback is gated off in control mode.
    """

    configured = os.environ.get("RESEARCH_PLUGIN_MODAL_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise BackendValidationError(f"RESEARCH_PLUGIN_MODAL_ENV_FILE does not exist: {path}")
    elif _env_discovery_disabled():
        return  # control mode: no implicit .env discovery
    else:
        # merv/backend/execution/backends/modal/config.py -> merv/
        path = Path(__file__).resolve().parents[4] / ".env"
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


def parse_modal_hints(
    *, backend_hints: Mapping[str, Any], config: ModalConfig
) -> ModalJobHints:
    raw = dict(backend_hints)
    unknown = sorted(set(raw) - _KNOWN_HINTS)
    if unknown:
        raise BackendValidationError(f"unknown Modal backend_hints: {', '.join(unknown)}")

    gpu = str(raw.get("gpu", DEFAULT_GPU)).upper()
    if gpu not in VALID_GPUS:
        raise BackendValidationError(f"invalid Modal gpu: {gpu}")

    compute_tier = str(raw.get("compute_tier", "default"))
    if compute_tier not in COMPUTE_TIERS:
        raise BackendValidationError(f"invalid Modal compute_tier: {compute_tier}")

    timeout = _positive_int(raw.get("timeout", config.job_timeout), field="timeout")
    config.validate_timeout_budget(job_timeout=timeout)

    return ModalJobHints(
        gpu=gpu,
        compute_tier=compute_tier,
        cuda_devel=_bool_hint(raw.get("cuda_devel", False), field="cuda_devel"),
        image_packages=_string_tuple(raw.get("image_packages", ()), field="image_packages"),
        timeout=timeout,
        experiment_path=_optional_repo_relative_dir(
            raw.get("experiment_path"), field="experiment_path"
        ),
        cloud=_optional_string(raw.get("cloud"), field="cloud"),
        region=_region_hint(raw.get("region")),
    )


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
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
    raw = os.environ.get(name)
    if raw is not None and not str(raw).strip():
        raise BackendValidationError(f"{name} must be an integer")
    try:
        parsed = env_int(name, default)
    except ValueError as exc:
        raise BackendValidationError(f"{name} must be an integer") from exc
    return parsed


def _positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BackendValidationError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise BackendValidationError(f"{field} must be positive")
    return parsed


def _bool_hint(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise BackendValidationError(f"{field} must be a boolean")


def _string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise BackendValidationError(f"{field} must be a list of strings")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BackendValidationError(f"{field} must be a list of non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _optional_string(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise BackendValidationError(f"{field} must be a string")
    return value.strip() or None


def _region_hint(value: Any) -> str | tuple[str, ...] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        return _string_tuple(value, field="region") or None
    raise BackendValidationError("region must be a string or list of strings")


def _optional_repo_relative_dir(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise BackendValidationError(f"{field} must be a string")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise BackendValidationError(f"{field} must be repo-relative and may not contain '..'")
    cleaned = rel.as_posix().rstrip("/")
    if cleaned in {"", "."}:
        raise BackendValidationError(f"{field} must identify a concrete experiment directory")
    if len(PurePosixPath(cleaned).parts) < 2:
        raise BackendValidationError(
            f"{field} must identify an experiment directory with at least two path components"
        )
    return cleaned or None


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
