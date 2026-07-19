"""Configuration for Thunder Compute API and VM access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .....kernel.env import env_value
from ....sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT, SESSIONS_DIRNAME


DEFAULT_BASE_URL = "https://api.thundercompute.com:8443/v1"
DEFAULT_TEMPLATE = "base"
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_SANDBOX_DATA_DIR = DEFAULT_DATA_DIR
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class ThunderCloudConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "ThunderCloudConfig":
        load_thunder_env_file()
        api_key = (
            env_value("MERV_THUNDER_API_KEY")
            or env_value("THUNDER_COMPUTE_API_KEY")
            or env_value("TNR_API_TOKEN")
            or ""
        )
        if not api_key:
            raise BackendValidationError(
                "Thunder Compute API key is required; set "
                "MERV_THUNDER_API_KEY, THUNDER_COMPUTE_API_KEY, or TNR_API_TOKEN"
            )
        base_url = env_value("MERV_THUNDER_API_BASE") or DEFAULT_BASE_URL
        parsed = urlsplit(base_url)
        if parsed.scheme != "https":
            localhost = parsed.scheme == "http" and parsed.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if not localhost:
                raise BackendValidationError(
                    "MERV_THUNDER_API_BASE must be an HTTPS URL "
                    "(http is only allowed for localhost tests)"
                )
        if not parsed.netloc:
            raise BackendValidationError("MERV_THUNDER_API_BASE must include a host")
        return cls(api_key=api_key, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class ThunderSandboxConfig:
    cloud: ThunderCloudConfig
    instance_type_name: str = ""
    template: str = DEFAULT_TEMPLATE
    ssh_user: str = DEFAULT_SSH_USER
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_SANDBOX_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "ThunderSandboxConfig":
        cloud = ThunderCloudConfig.from_env()
        remote_root = _absolute_posix_path(
            env_value("MERV_THUNDER_WORKDIR") or DEFAULT_REMOTE_ROOT,
            field="MERV_THUNDER_WORKDIR",
        )
        sandbox_data_dir = _absolute_posix_path(
            env_value("MERV_THUNDER_DATA_DIR") or DEFAULT_SANDBOX_DATA_DIR,
            field="MERV_THUNDER_DATA_DIR",
        )
        _validate_data_dir(
            sandbox_data_dir,
            remote_root=remote_root,
            field="MERV_THUNDER_DATA_DIR",
        )
        return cls(
            cloud=cloud,
            instance_type_name=env_value("MERV_THUNDER_INSTANCE_TYPE") or "",
            template=env_value("MERV_THUNDER_TEMPLATE") or DEFAULT_TEMPLATE,
            ssh_user=env_value("MERV_THUNDER_SSH_USER") or DEFAULT_SSH_USER,
            remote_root=remote_root,
            sandbox_data_dir=sandbox_data_dir,
            poll_timeout_seconds=_positive_int(
                env_value("MERV_THUNDER_POLL_TIMEOUT")
                or DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS,
                field="MERV_THUNDER_POLL_TIMEOUT",
            ),
            poll_interval_seconds=_positive_float(
                env_value("MERV_THUNDER_POLL_INTERVAL")
                or DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS,
                field="MERV_THUNDER_POLL_INTERVAL",
            ),
        )


def load_thunder_env_file() -> None:
    """Load Thunder settings from an explicit env file or local checkout .env."""

    configured = env_value("MERV_THUNDER_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise BackendValidationError(f"MERV_THUNDER_ENV_FILE does not exist: {path}")
    elif (env_value("MERV_MODE") or "").lower() == "control":
        return
    else:
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


def _absolute_posix_path(value: str, *, field: str) -> str:
    value = value.strip()
    if not value.startswith("/"):
        raise BackendValidationError(f"{field} must be an absolute POSIX path")
    return value.rstrip("/") or "/"


def _is_under_path(child: str, parent: str) -> bool:
    child = child.rstrip("/")
    parent = parent.rstrip("/")
    return child == parent or child.startswith(parent + "/")


def _validate_data_dir(data_dir: str, *, remote_root: str, field: str) -> None:
    root = remote_root.rstrip("/")
    if data_dir.rstrip("/") == root:
        raise BackendValidationError(f"{field} must not equal the remote root {root}")
    if _is_under_path(data_dir, root):
        first = data_dir.rstrip("/")[len(root) + 1 :].split("/", 1)[0]
        if first.startswith("exp_") or first == SESSIONS_DIRNAME:
            raise BackendValidationError(
                f"{field} must not collide with per-experiment folders or "
                f"{SESSIONS_DIRNAME} under the remote root"
            )


def _positive_int(value: object, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BackendValidationError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise BackendValidationError(f"{field} must be a positive integer")
    return parsed


def _positive_float(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BackendValidationError(f"{field} must be positive") from exc
    if parsed <= 0:
        raise BackendValidationError(f"{field} must be positive")
    return parsed
