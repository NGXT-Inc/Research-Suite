"""Configuration for Lambda Labs Cloud API and VM access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .._config import _env_discovery_disabled
from .._config import _absolute_posix_path, _load_env_text, _positive_float, _positive_int, _validate_data_dir
from .....kernel.env import env_value
from ....sandbox_backend import BackendValidationError
from ....sandbox_paths import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT

DEFAULT_BASE_URL = "https://cloud.lambda.ai/api/v1"
DEFAULT_SANDBOX_DATA_DIR = DEFAULT_DATA_DIR
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class LambdaCloudConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "LambdaCloudConfig":
        load_lambda_env_file()
        api_key = (
            env_value("MERV_LAMBDA_API_KEY")
            or env_value("LAMBDA_LABS_API_KEY")
            or env_value("LAMBDA_API_KEY")
            or ""
        )
        if not api_key:
            raise BackendValidationError(
                "Lambda Cloud API key is required; set MERV_LAMBDA_API_KEY, "
                "LAMBDA_LABS_API_KEY, or LAMBDA_API_KEY"
            )
        base_url = env_value("MERV_LAMBDA_API_BASE") or DEFAULT_BASE_URL
        if not base_url.startswith(("http://", "https://")):
            raise BackendValidationError("MERV_LAMBDA_API_BASE must be an HTTP URL")
        return cls(api_key=api_key, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class LambdaSandboxConfig:
    cloud: LambdaCloudConfig
    # Region + instance type are *optional fallback defaults*. The agent chooses
    # the machine per request (sandbox.request instance_type/region); these env
    # values only fill in when a request omits them. Empty means "let the agent
    # pick from live availability" — sandbox.request returns a selection menu.
    region_name: str = ""
    instance_type_name: str = ""
    ssh_user: str = DEFAULT_SSH_USER
    # Remote root under which each experiment's one synced folder
    # (`<root>/<experiment_id>`) is created.
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_SANDBOX_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "LambdaSandboxConfig":
        cloud = LambdaCloudConfig.from_env()
        region_name = _first_env(
            "MERV_LAMBDA_REGION",
            "LAMBDA_LABS_REGION",
            "LAMBDA_REGION",
        )
        instance_type_name = _first_env(
            "MERV_LAMBDA_INSTANCE_TYPE",
            "LAMBDA_LABS_INSTANCE_TYPE",
            "LAMBDA_INSTANCE_TYPE",
        )
        remote_root = _absolute_posix_path(
            env_value("MERV_LAMBDA_WORKDIR") or DEFAULT_REMOTE_ROOT,
            field="MERV_LAMBDA_WORKDIR",
        )
        sandbox_data_dir = _absolute_posix_path(
            env_value("MERV_LAMBDA_DATA_DIR") or DEFAULT_SANDBOX_DATA_DIR,
            field="MERV_LAMBDA_DATA_DIR",
        )
        _validate_data_dir(sandbox_data_dir, remote_root=remote_root, field="MERV_LAMBDA_DATA_DIR")
        return cls(
            cloud=cloud,
            region_name=region_name,
            instance_type_name=instance_type_name,
            ssh_user=env_value("MERV_LAMBDA_SSH_USER") or DEFAULT_SSH_USER,
            remote_root=remote_root,
            sandbox_data_dir=sandbox_data_dir,
            poll_timeout_seconds=_positive_int(
                env_value("MERV_LAMBDA_POLL_TIMEOUT")
                or DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS,
                field="MERV_LAMBDA_POLL_TIMEOUT",
            ),
            poll_interval_seconds=_positive_float(
                env_value("MERV_LAMBDA_POLL_INTERVAL")
                or DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS,
                field="MERV_LAMBDA_POLL_INTERVAL",
            ),
        )


def load_lambda_env_file() -> None:
    """Load Lambda credentials/settings from the configured plugin env file.

    Only an EXPLICITLY configured env file is ever read (no implicit package-root
    ``.env`` fallback), so this is already the secret-store seam: in control mode
    point ``MERV_LAMBDA_ENV_FILE`` at a mounted secret. Control mode
    additionally refuses to fall through to the Modal env-file alias so a user
    machine's ``MERV_MODAL_ENV_FILE`` can't smuggle creds into the
    cloud — the control plane reads its own env / secret store only.
    """

    configured = env_value("MERV_LAMBDA_ENV_FILE")
    if not configured and not _env_discovery_disabled():
        configured = env_value("MERV_MODAL_ENV_FILE")
    if not configured:
        return
    path = Path(configured).expanduser()
    if not path.exists():
        raise BackendValidationError(f"Lambda env file does not exist: {path}")
    _load_env_text(path.read_text())


def _first_env(*names: str) -> str:
    for name in names:
        value = env_value(name)
        if value:
            return value
    return ""
