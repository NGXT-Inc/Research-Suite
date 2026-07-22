"""Configuration for the Voltage Park cloud API and VM access."""

from __future__ import annotations

from dataclasses import dataclass

from .....kernel.env import env_value
from ....sandbox_backend import BackendValidationError
from ....sandbox_paths import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT


DEFAULT_BASE_URL = "https://cloud-api.voltagepark.com/api/v1"
DEFAULT_SSH_USER = "root"
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class VoltageParkCloudConfig:
    token: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "VoltageParkCloudConfig":
        token = (
            env_value("MERV_VOLTAGE_PARK_TOKEN")
            or env_value("VOLTAGE_PARK_TOKEN")
            or ""
        ).strip()
        if not token:
            raise BackendValidationError(
                "Voltage Park API token is required; set "
                "MERV_VOLTAGE_PARK_TOKEN or VOLTAGE_PARK_TOKEN"
            )
        base_url = (
            env_value("MERV_VOLTAGE_PARK_API_BASE") or DEFAULT_BASE_URL
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise BackendValidationError(
                "MERV_VOLTAGE_PARK_API_BASE must be an HTTP URL"
            )
        return cls(token=token, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class VoltageParkSandboxConfig:
    cloud: VoltageParkCloudConfig
    ssh_user: str = DEFAULT_SSH_USER
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "VoltageParkSandboxConfig":
        return cls(
            cloud=VoltageParkCloudConfig.from_env(),
            ssh_user=(
                env_value("MERV_VOLTAGE_PARK_SSH_USER") or DEFAULT_SSH_USER
            ).strip()
            or DEFAULT_SSH_USER,
        )
