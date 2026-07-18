"""Configuration for the TensorDock v2 API and VM access."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ....sandbox.sandbox_backend import BackendValidationError
from ...sync_dirs import DEFAULT_DATA_DIR, DEFAULT_REMOTE_ROOT


DEFAULT_BASE_URL = "https://dashboard.tensordock.com/api/v2"
DEFAULT_IMAGE = "ubuntu2404"
# cloud-init runs as root and the bootstrap authorizes root's key; the image
# default user varies by host, so root is the stable principal.
DEFAULT_SSH_USER = "root"
DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS = 900
DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class TensorDockCloudConfig:
    token: str
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "TensorDockCloudConfig":
        token = (
            os.environ.get("RESEARCH_PLUGIN_TENSORDOCK_TOKEN")
            or os.environ.get("TENSORDOCK_TOKEN")
            or ""
        ).strip()
        if not token:
            raise BackendValidationError(
                "TensorDock API token is required; set "
                "RESEARCH_PLUGIN_TENSORDOCK_TOKEN or TENSORDOCK_TOKEN"
            )
        base_url = (
            os.environ.get("RESEARCH_PLUGIN_TENSORDOCK_API_BASE") or DEFAULT_BASE_URL
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise BackendValidationError(
                "RESEARCH_PLUGIN_TENSORDOCK_API_BASE must be an HTTP URL"
            )
        return cls(token=token, base_url=base_url.rstrip("/"))


@dataclass(frozen=True)
class TensorDockSandboxConfig:
    cloud: TensorDockCloudConfig
    image: str = DEFAULT_IMAGE
    ssh_user: str = DEFAULT_SSH_USER
    remote_root: str = DEFAULT_REMOTE_ROOT
    sandbox_data_dir: str = DEFAULT_DATA_DIR
    poll_timeout_seconds: int = DEFAULT_INSTANCE_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_INSTANCE_POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> "TensorDockSandboxConfig":
        return cls(
            cloud=TensorDockCloudConfig.from_env(),
            image=(
                os.environ.get("RESEARCH_PLUGIN_TENSORDOCK_IMAGE") or DEFAULT_IMAGE
            ).strip(),
            ssh_user=(
                os.environ.get("RESEARCH_PLUGIN_TENSORDOCK_SSH_USER") or DEFAULT_SSH_USER
            ).strip()
            or DEFAULT_SSH_USER,
        )
