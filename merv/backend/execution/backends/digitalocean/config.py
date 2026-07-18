"""Configuration for the DigitalOcean API and GPU droplet access."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ....sandbox.sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT


DEFAULT_BASE_URL = "https://api.digitalocean.com/v2"
# The AI/ML-ready Ubuntu image (NVIDIA drivers preinstalled). Plain Ubuntu
# slugs boot GPU droplets too but ship no drivers; override for CPU sizes.
DEFAULT_IMAGE = "gpu-h100x1-base"
DEFAULT_SSH_USER = "root"
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class DigitalOceanCloudConfig:
    token: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "DigitalOceanCloudConfig":
        token = (
            os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_TOKEN")
            or os.environ.get("DIGITALOCEAN_TOKEN")
            or os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
            or ""
        ).strip()
        if not token:
            raise BackendValidationError(
                "DigitalOcean API token is required; set "
                "RESEARCH_PLUGIN_DIGITALOCEAN_TOKEN, DIGITALOCEAN_TOKEN, or "
                "DIGITALOCEAN_ACCESS_TOKEN"
            )
        base_url = (
            os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_API_BASE") or DEFAULT_BASE_URL
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise BackendValidationError(
                "RESEARCH_PLUGIN_DIGITALOCEAN_API_BASE must be an HTTP URL"
            )
        return cls(token=token, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class DigitalOceanSandboxConfig:
    cloud: DigitalOceanCloudConfig
    image: str = DEFAULT_IMAGE
    region: str = ""
    size: str = ""
    ssh_user: str = DEFAULT_SSH_USER
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "DigitalOceanSandboxConfig":
        return cls(
            cloud=DigitalOceanCloudConfig.from_env(),
            image=(
                os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_IMAGE") or DEFAULT_IMAGE
            ).strip(),
            region=(os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_REGION") or "").strip(),
            size=(os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_SIZE") or "").strip(),
            ssh_user=(
                os.environ.get("RESEARCH_PLUGIN_DIGITALOCEAN_SSH_USER") or DEFAULT_SSH_USER
            ).strip()
            or DEFAULT_SSH_USER,
        )
