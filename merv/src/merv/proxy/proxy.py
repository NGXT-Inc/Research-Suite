"""Composition edge for the stdlib-only Merv MCP proxy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from merv.shared.client_config import HOSTED_CONTROL_URL

from . import __version__
from .credential_provider import CredentialProvider
from .errors import is_loopback_url
from .http_client import StdlibHttpClient
from .mcp_shell import McpShell
from .project_scope import ProjectScope
from .routing import ToolRoute
from .tool_gateway import ToolGateway, storage_feature_enabled


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_CONTROL_URL = HOSTED_CONTROL_URL
_STATIC_CATALOG_PATH = Path(__file__).with_name("_tool_catalog.json")
_PROXY_MANIFEST_PATH = Path(__file__).with_name("_tool_manifest.json")

# Source-compatible names retained for launchers and catalog tests.
_ToolMeta = ToolRoute
_storage_feature_enabled = storage_feature_enabled
_is_loopback_url = is_loopback_url


@dataclass(frozen=True)
class ProxyConfig:
    repo_root: Path
    control_url: str | None
    project_links_path: Path | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    api_key: str = ""
    client_config_path: Path | None = None

    def with_url(self, url: str) -> "ProxyConfig":
        return ProxyConfig(
            repo_root=self.repo_root,
            control_url=url,
            project_links_path=self.project_links_path,
            timeout_seconds=self.timeout_seconds,
            api_key=self.api_key,
            client_config_path=self.client_config_path,
        )


class HttpProxyMcpServer(McpShell, ToolGateway):
    """Small MCP shell composed with manifest routing and explicit adapters."""

    def __init__(self, *, config: ProxyConfig) -> None:
        self.config = config
        self._tool_cache: dict[str, ToolRoute] | None = None
        self._local_data_plane = None
        opener = lambda request, timeout: urlopen(request, timeout=timeout)
        self._credentials = CredentialProvider(
            api_key=config.api_key,
            control_url=config.control_url,
            config_path=config.client_config_path,
            timeout_seconds=config.timeout_seconds,
            opener=opener,
        )
        self._http = StdlibHttpClient(
            control_url=config.control_url,
            timeout_seconds=config.timeout_seconds,
            headers=lambda is_cloud: self._credentials.headers(
                is_cloud=is_cloud,
                client_version=__version__,
            ),
            opener=opener,
        )
        self._project_scope = ProjectScope(
            repo_root=config.repo_root,
            links_path=config.project_links_path,
        )
