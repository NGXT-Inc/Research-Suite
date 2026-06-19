"""Entrypoint for the Research Plugin MCP stdio proxy.

The MCP process is a thin adapter — it owns no state and starts no jobs.
It forwards Codex tool calls plus hidden repo context to the long-running HTTP
daemon. See ``docs/STARTUP_CHEATSHEET.md`` for the startup order.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .daemon_marker import discover_daemon_url
from .proxy import DEFAULT_DAEMON_URL, HttpProxyMcpServer, ProxyConfig


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
    parser.add_argument(
        "--control-url",
        default=os.environ.get("RESEARCH_PLUGIN_CONTROL_URL"),
        help="Cloud control-plane URL (split mode). Unset ⇒ single-upstream local mode.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    daemon_url = args.daemon_url or discover_daemon_url(repo_root=repo_root)
    # Split-transport config (cloud plan Phase 8, §3.4). The cloud bearer token
    # and the daemon loopback secret are read from files so they never sit in a
    # process arg or env value that gets logged. None ⇒ local mode.
    control_url = (args.control_url or "").rstrip("/") or None
    token = _read_secret_file(os.environ.get("RESEARCH_PLUGIN_CONTROL_TOKEN_FILE"))
    daemon_secret_file = os.environ.get("RESEARCH_PLUGIN_DAEMON_SECRET_FILE")
    if control_url and not daemon_secret_file:
        daemon_secret_file = str(Path.home() / ".research_plugin" / "daemon_secret")
    daemon_secret = _read_secret_file(daemon_secret_file)

    if not daemon_url and not control_url:
        # Don't hard-exit: Codex will call initialize before anything else, and
        # the daemon may come up between launches. The proxy returns a clear
        # error envelope per tool call if the daemon is still missing then.
        sys.stderr.write(
            "[research_plugin] no HTTP daemon detected; tool calls will fail "
            f"until you start one with `research-plugin-http` at {DEFAULT_DAEMON_URL} "
            "or set RESEARCH_PLUGIN_DAEMON_URL to the shared daemon URL.\n"
        )

    config = ProxyConfig(
        repo_root=repo_root,
        daemon_url=daemon_url,
        control_url=control_url,
        token=token,
        daemon_secret=daemon_secret,
    )
    HttpProxyMcpServer(config=config).serve()
    return 0


def _read_secret_file(path: str | None) -> str | None:
    """Read a token/secret from a file (JSON {"token": ...} or a bare line).

    Never returns the value from an env var directly — secrets live in 0600
    files so they don't leak into logged process state. None when unconfigured.
    """
    if not path:
        return None
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        import json

        parsed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(parsed, dict):
        token = parsed.get("token") or parsed.get("secret")
        return str(token) if token else None
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
