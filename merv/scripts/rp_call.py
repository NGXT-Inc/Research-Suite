#!/usr/bin/env python3
"""Tiny driver that calls Merv MCP tools through the real proxy.

Reuses mcp_server.proxy (the same brain routing, version, project-link, and
checkout-local data-plane logic an MCP client uses), so this is a faithful
stand-in for a client-launched stdio server. Usage:

    rp_call.py list                       # tools/list (names + planes)
    rp_call.py schema <tool>              # full input schema for one tool
    rp_call.py call <tool> '<json-args>'  # tools/call, prints the result JSON

Env: MERV_REPO_ROOT picks the project working dir (defaults to CWD).
MERV_CONTROL_URL overrides the machine brain URL stored in
the machine client config (~/.merv/client.json, or the legacy
~/.research_plugin/client.json when that dir exists); an unconfigured machine
uses the hosted brain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from research_plugin_shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    dual_env_value,
    read_client_config,
    resolve_client_config_path,
)
from mcp_server.project_links import default_project_links_path
from mcp_server.proxy import DEFAULT_CONTROL_URL, HttpProxyMcpServer, ProxyConfig


def _build_server() -> HttpProxyMcpServer:
    repo_root = Path(dual_env_value("MERV_REPO_ROOT") or ".").resolve()
    cfg_path = resolve_client_config_path()
    cc = read_client_config({CLIENT_CONFIG_ENV_VAR: str(cfg_path)})
    control_url = (
        dual_env_value("MERV_CONTROL_URL") or cc.get("control_url", "")
    ).rstrip("/") or DEFAULT_CONTROL_URL
    cfg = ProxyConfig(
        repo_root=repo_root,
        control_url=control_url,
        project_links_path=default_project_links_path(
            client_config=cc,
            config_path=cfg_path,
        ),
    )
    return HttpProxyMcpServer(config=cfg)


def _rpc(server: HttpProxyMcpServer, method: str, params: dict) -> dict:
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return resp or {}


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    server = _build_server()
    cmd = args[0]
    if cmd == "list":
        resp = _rpc(server, "tools/list", {})
        tools = (resp.get("result") or {}).get("tools", [])
        for t in tools:
            plane = t.get("plane") or t.get("_plane") or "?"
            print(f"{t.get('name'):28} plane={plane}")
        print(f"\n[{len(tools)} tools]")
        return 0
    if cmd == "schema":
        resp = _rpc(server, "tools/list", {})
        tools = (resp.get("result") or {}).get("tools", [])
        for t in tools:
            if t.get("name") == args[1]:
                print(json.dumps(t, indent=2))
                return 0
        print(f"tool not found: {args[1]}", file=sys.stderr)
        return 1
    if cmd == "call":
        name = args[1]
        call_args = json.loads(args[2]) if len(args) > 2 and args[2] else {}
        resp = _rpc(server, "tools/call", {"name": name, "arguments": call_args})
        print(json.dumps(resp, indent=2, default=str))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
