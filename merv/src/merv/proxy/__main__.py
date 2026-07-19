"""Entrypoint for the Merv MCP stdio proxy.

The MCP process is the local data-plane adapter. It always talks to one brain
URL and performs repo-local file work itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from merv.shared.client_config import (
    API_KEY_ENV_VAR,
    CLIENT_CONFIG_ENV_VAR,
    CONTROL_URL_ENV_VAR,
    dual_env_value,
    read_client_config,
    resolve_client_config_path,
)

from .project_links import ProjectLinks, default_project_links_path
from .proxy import DEFAULT_CONTROL_URL, HttpProxyMcpServer, ProxyConfig, _is_loopback_url


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="merv-mcp",
        description="Stdio MCP proxy for the Merv brain service.",
    )
    parser.add_argument(
        "--repo",
        default=dual_env_value("MERV_REPO_ROOT") or ".",
        help="Research repo used for proxy-local file work and project linking.",
    )
    parser.add_argument(
        "--control-url",
        default=dual_env_value(CONTROL_URL_ENV_VAR),
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
    # RapidReview API key for the hosted brain: env beats machine config,
    # mirroring the control_url chain. Absent against a loopback brain.
    api_key = (
        dual_env_value(API_KEY_ENV_VAR) or client_config.get("api_key", "")
    ).strip()
    if _is_loopback_url(control_url):
        sys.stderr.write(
            "[merv] using local brain URL "
            f"{control_url}; start it with `merv-http`.\n"
        )
    elif control_url == DEFAULT_CONTROL_URL:
        sys.stderr.write(
            f"[merv] using hosted brain {DEFAULT_CONTROL_URL} "
            "(default; override with `merv-client configure "
            "--control-url ...` or MERV_CONTROL_URL).\n"
        )

    config = ProxyConfig(
        repo_root=repo_root,
        control_url=control_url,
        project_links_path=project_links_path,
        api_key=api_key,
        client_config_path=client_config_path,
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
