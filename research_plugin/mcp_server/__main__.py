"""Entrypoint for the Research Plugin MCP stdio proxy.

The MCP process is the local data-plane adapter. It always talks to one brain
URL and performs repo-local file work itself.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from research_plugin_shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    read_client_config,
    resolve_client_config_path,
)

from .project_links import ProjectLinks, default_project_links_path
from .proxy import DEFAULT_CONTROL_URL, HttpProxyMcpServer, ProxyConfig, _is_loopback_url


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="research-plugin-mcp",
        description="Stdio MCP proxy for the research_plugin brain service.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("RESEARCH_PLUGIN_REPO_ROOT", "."),
        help="Research repo used for proxy-local file work and project linking.",
    )
    parser.add_argument(
        "--control-url",
        default=os.environ.get("RESEARCH_PLUGIN_CONTROL_URL"),
        help=f"Brain/control-plane URL. Defaults to the hosted brain ({DEFAULT_CONTROL_URL}).",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    client_config_path = resolve_client_config_path()
    client_config = read_client_config({CLIENT_CONFIG_ENV_VAR: str(client_config_path)})
    project_links_path = default_project_links_path(
        client_config=client_config,
        config_path=client_config_path,
    )
    linked_client_config = (
        client_config
        if _repo_is_linked(db_path=project_links_path, repo_root=repo_root)
        else {}
    )
    control_url = (
        args.control_url
        or linked_client_config.get("control_url", "")
        or client_config.get("control_url", "")
        or DEFAULT_CONTROL_URL
    ).rstrip("/")
    if _is_loopback_url(control_url):
        sys.stderr.write(
            "[research_plugin] using local brain URL "
            f"{control_url}; start it with `research-plugin-http`.\n"
        )
    elif control_url == DEFAULT_CONTROL_URL:
        sys.stderr.write(
            f"[research_plugin] using hosted brain {DEFAULT_CONTROL_URL} "
            "(default; override with `research-plugin-client configure "
            "--control-url ...` or RESEARCH_PLUGIN_CONTROL_URL).\n"
        )

    config = ProxyConfig(
        repo_root=repo_root,
        control_url=control_url,
        project_links_path=project_links_path,
    )
    HttpProxyMcpServer(config=config).serve()
    return 0


def _repo_is_linked(*, db_path: Path, repo_root: Path) -> bool:
    try:
        return bool(ProjectLinks(db_path=db_path).project_for_repo(repo_root=str(repo_root)))
    except Exception:  # noqa: BLE001 - corrupt link DB should not kill initialize.
        return False


if __name__ == "__main__":
    raise SystemExit(main())
