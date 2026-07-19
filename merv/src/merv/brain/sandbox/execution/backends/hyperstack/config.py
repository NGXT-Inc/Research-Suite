"""Configuration for the Hyperstack (NexGen Cloud) API and VM access."""

from __future__ import annotations

from dataclasses import dataclass

from .....kernel.env import env_value
from ....sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT


DEFAULT_BASE_URL = "https://infrahub-api.nexgencloud.com/v1"
# Exact image name from the Hyperstack docs; override for CUDA-preinstalled
# variants (e.g. "Ubuntu Server 22.04 LTS R535 CUDA 12.2").
DEFAULT_IMAGE_NAME = "Ubuntu Server 24.04 LTS (Noble Numbat)"
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class HyperstackCloudConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "HyperstackCloudConfig":
        api_key = (
            env_value("MERV_HYPERSTACK_API_KEY")
            or env_value("HYPERSTACK_API_KEY")
            or ""
        ).strip()
        if not api_key:
            raise BackendValidationError(
                "Hyperstack API key is required; set "
                "MERV_HYPERSTACK_API_KEY or HYPERSTACK_API_KEY"
            )
        base_url = (
            env_value("MERV_HYPERSTACK_API_BASE") or DEFAULT_BASE_URL
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise BackendValidationError(
                "MERV_HYPERSTACK_API_BASE must be an HTTP URL"
            )
        return cls(api_key=api_key, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class HyperstackSandboxConfig:
    cloud: HyperstackCloudConfig
    # Every Hyperstack VM and keypair lives inside a user-created environment
    # (made once in the console); the environment also fixes the region.
    environment_name: str = ""
    image_name: str = DEFAULT_IMAGE_NAME
    flavor_name: str = ""
    ssh_user: str = DEFAULT_SSH_USER
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "HyperstackSandboxConfig":
        environment_name = (
            env_value("MERV_HYPERSTACK_ENVIRONMENT") or ""
        ).strip()
        if not environment_name:
            raise BackendValidationError(
                "Hyperstack requires an environment; create one in the console "
                "(it pins the region) and set MERV_HYPERSTACK_ENVIRONMENT"
            )
        return cls(
            cloud=HyperstackCloudConfig.from_env(),
            environment_name=environment_name,
            image_name=(
                env_value("MERV_HYPERSTACK_IMAGE") or DEFAULT_IMAGE_NAME
            ).strip(),
            flavor_name=(
                env_value("MERV_HYPERSTACK_FLAVOR") or ""
            ).strip(),
            ssh_user=(
                env_value("MERV_HYPERSTACK_SSH_USER") or DEFAULT_SSH_USER
            ).strip()
            or DEFAULT_SSH_USER,
        )
