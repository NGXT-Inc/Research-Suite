"""Entrypoint for the Research Plugin MCP stdio proxy.

The MCP process is a thin adapter — it owns no state and starts no jobs.
It forwards Codex tool calls to the long-running HTTP daemon that the user
started in the target repo. See ``docs/STARTUP_CHEATSHEET.md`` for the
startup order.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .daemon_marker import discover_daemon_url
from .proxy import HttpProxyMcpServer, ProxyConfig


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="research-plugin-mcp",
        description="Stdio MCP proxy for the research_plugin HTTP daemon.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("RESEARCH_PLUGIN_REPO_ROOT", "."),
        help="Research repo whose .research_plugin/daemon.json should be read.",
    )
    parser.add_argument(
        "--daemon-url",
        default=os.environ.get("RESEARCH_PLUGIN_DAEMON_URL"),
        help="Override the daemon URL (host:port). If unset, discovery uses the repo marker.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    daemon_url = args.daemon_url or discover_daemon_url(repo_root=repo_root)

    if not daemon_url:
        # Don't hard-exit: Codex will call initialize before anything else, and
        # the daemon may come up between launches. The proxy returns a clear
        # error envelope per tool call if the daemon is still missing then.
        sys.stderr.write(
            "[research_plugin] no HTTP daemon detected; tool calls will fail "
            "until you start one with `research-plugin-http --repo "
            f"{repo_root}`.\n"
        )

    config = ProxyConfig(repo_root=repo_root, daemon_url=daemon_url)
    HttpProxyMcpServer(config=config).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
