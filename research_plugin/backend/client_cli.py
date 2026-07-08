"""Machine-level CLI for hosted-control + local-data split mode."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from research_plugin_shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    DAEMON_STATE_DIR_ENV_VAR,
    HOSTED_CONTROL_URL,
    LOCAL_BRAIN_URL,
    read_client_config,
    resolve_client_config_path,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ClientError as exc:
        print(f"research-plugin-client: {exc}", file=sys.stderr)
        return 2


class ClientError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-plugin-client",
        description="Configure hosted control and link local folders to Research Plugin projects.",
    )
    parser.add_argument(
        "--config",
        help="Machine client config path (default: ~/.research_plugin/client.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser(
        "configure",
        aliases=["login"],
        help="Save hosted-control URL for this machine.",
    )
    _add_control_args(configure)
    configure.set_defaults(func=_cmd_configure)

    link = sub.add_parser(
        "link",
        help="Link a local repo folder to an existing hosted project_id.",
    )
    link.add_argument("--project-id", required=True, help="Hosted control-plane project id.")
    link.add_argument("--repo", default=".", help="Local repo folder to link (default: cwd).")
    link.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)
    link.set_defaults(func=_cmd_link)

    connect = sub.add_parser(
        "connect",
        help="Configure/login and optionally link the current folder.",
    )
    _add_control_args(connect, required=False)
    connect.add_argument("--project-id", help="Existing hosted project id to link.")
    connect.add_argument("--repo", default=".", help="Local repo folder to link (default: cwd).")
    connect.set_defaults(func=_cmd_connect)

    route = sub.add_parser("route", help="Show the project linked to a local repo folder.")
    route.add_argument("--repo", default=".", help="Local repo folder (default: cwd).")
    route.set_defaults(func=_cmd_route)

    links = sub.add_parser("links", help="List local folder links on this machine.")
    links.set_defaults(func=_cmd_links)

    unlink = sub.add_parser("unlink", help="Remove one local folder link.")
    unlink.add_argument("--repo", default=".", help="Local repo folder to unlink (default: cwd).")
    unlink.set_defaults(func=_cmd_unlink)

    env = sub.add_parser(
        "mcp-env",
        help="Print environment variables a manual MCP config should use.",
    )
    env.add_argument("--repo", default=".", help="Local repo folder (default: cwd).")
    env.set_defaults(func=_cmd_mcp_env)
    return parser


def _add_control_args(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "--control-url",
        required=False,
        default=None,
        help=(
            f"Brain/control-plane URL. Defaults to the hosted brain "
            f"({HOSTED_CONTROL_URL}); use {LOCAL_BRAIN_URL} for a local "
            "deployment."
        ),
    )


def _cmd_configure(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    existing = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    config = configure_client(
        config_path=config_path,
        control_url=args.control_url or HOSTED_CONTROL_URL,
    )
    _print_configured(config_path=config_path, config=config)
    return 0


def _cmd_connect(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    existing = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    if _has_control_config_args(args):
        control_url = args.control_url or existing.get("control_url", "")
        config = configure_client(
            config_path=config_path,
            control_url=control_url or HOSTED_CONTROL_URL,
        )
        _print_configured(config_path=config_path, config=config)
    if args.project_id:
        _ensure_default_config(config_path)
        repo = _repo(args.repo)
        link_repo(
            config_path=config_path,
            repo_root=repo,
            project_id=args.project_id,
        )
        print(f"linked {repo} -> {args.project_id}")
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    del args.no_start
    _ensure_default_config(config_path)
    repo = _repo(args.repo)
    link_repo(config_path=config_path, repo_root=repo, project_id=args.project_id)
    print(f"linked {repo} -> {args.project_id}")
    return 0


def _cmd_route(args: argparse.Namespace) -> int:
    route = route_repo(config_path=_config_path(args), repo_root=_repo(args.repo))
    print(json.dumps(route, indent=2, sort_keys=True))
    return 0 if route.get("exists") else 1


def _cmd_links(args: argparse.Namespace) -> int:
    links = list_links(config_path=_config_path(args))
    print(json.dumps(links, indent=2, sort_keys=True))
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    result = unlink_repo(config_path=_config_path(args), repo_root=_repo(args.repo))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cmd_mcp_env(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    config = _ensure_default_config(config_path)
    repo = _repo(args.repo)
    env = {
        "RESEARCH_PLUGIN_REPO_ROOT": str(repo),
        "RESEARCH_PLUGIN_CONTROL_URL": config["control_url"],
        DAEMON_STATE_DIR_ENV_VAR: config["daemon_state_dir"],
        CLIENT_CONFIG_ENV_VAR: str(config_path),
    }
    for key, value in env.items():
        print(f"{key}={value}")
    return 0


def configure_client(
    *,
    config_path: Path,
    control_url: str,
) -> dict[str, str]:
    control_url = (control_url or HOSTED_CONTROL_URL).strip()
    existing = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    config_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_state_dir = Path(
        existing.get("daemon_state_dir") or config_path.parent
    ).expanduser().resolve()
    config = {
        "control_url": control_url.rstrip("/"),
        "daemon_state_dir": str(daemon_state_dir),
    }
    _write_json_private(config_path, config)
    return config


def link_repo(*, config_path: Path, repo_root: Path, project_id: str) -> dict[str, Any]:
    if not project_id:
        raise ClientError("project_id is required")
    links = _project_links(config_path=config_path)
    canonical = str(repo_root.expanduser().resolve())
    conn = _links_connect(links)
    try:
        with conn:
            conn.execute(
                "INSERT INTO project_links (repo_root, project_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(repo_root) DO UPDATE SET "
                "project_id = excluded.project_id",
                (canonical, project_id, _now_iso()),
            )
    finally:
        conn.close()
    return {"linked": True, "repo_root": canonical, "project_id": project_id}


def route_repo(*, config_path: Path, repo_root: Path) -> dict[str, Any]:
    links = _project_links(config_path=config_path)
    canonical = str(repo_root.expanduser().resolve())
    conn = _links_connect(links)
    try:
        row = conn.execute(
            "SELECT project_id FROM project_links WHERE repo_root = ?",
            (canonical,),
        ).fetchone()
    finally:
        conn.close()
    project_id = str(row["project_id"]) if row is not None else None
    return {"exists": bool(project_id), "repo_root": canonical, "project_id": project_id}


def list_links(*, config_path: Path) -> dict[str, Any]:
    conn = _links_connect(_project_links(config_path=config_path))
    try:
        rows = conn.execute(
            "SELECT repo_root, project_id, created_at FROM project_links ORDER BY repo_root"
        ).fetchall()
    finally:
        conn.close()
    return {
        "links": [
            {
                "repo_root": str(row["repo_root"]),
                "project_id": str(row["project_id"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
    }


def unlink_repo(*, config_path: Path, repo_root: Path) -> dict[str, Any]:
    canonical = str(repo_root.expanduser().resolve())
    conn = _links_connect(_project_links(config_path=config_path))
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM project_links WHERE repo_root = ?", (canonical,)
            )
    finally:
        conn.close()
    return {"unlinked": bool(cur.rowcount), "repo_root": canonical}


def _config_path(args: argparse.Namespace) -> Path:
    if getattr(args, "config", None):
        return Path(args.config).expanduser().resolve()
    return resolve_client_config_path()


def _require_config(config_path: Path) -> dict[str, str]:
    config = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    missing = [key for key in ("control_url",) if not config.get(key)]
    if missing:
        raise ClientError(
            "machine is not configured; run "
            "research-plugin-client configure --control-url ..."
        )
    state_dir = str(_state_dir(config_path=config_path, config=config))
    config.setdefault("daemon_state_dir", state_dir)
    return config


def _ensure_default_config(config_path: Path) -> dict[str, str]:
    config = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    if config.get("control_url"):
        state_dir = str(_state_dir(config_path=config_path, config=config))
        config.setdefault("daemon_state_dir", state_dir)
        return config
    return configure_client(config_path=config_path, control_url=HOSTED_CONTROL_URL)


def _state_dir(*, config_path: Path, config: Mapping[str, str]) -> Path:
    raw = (config.get("daemon_state_dir") or "").strip()
    return Path(raw).expanduser().resolve() if raw else config_path.parent


def _project_links(*, config_path: Path) -> Path:
    config = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    return _state_dir(config_path=config_path, config=config) / "project_links.sqlite"


def _links_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(
        """
CREATE TABLE IF NOT EXISTS project_links (
  repo_root TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""
    )
    conn.commit()
    return conn


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _has_control_config_args(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "control_url", None))


def _write_json_private(path: Path, payload: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _repo(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise ClientError(f"repo path does not exist: {path}")
    if not path.is_dir():
        raise ClientError(f"repo path is not a directory: {path}")
    return path


def _print_configured(*, config_path: Path, config: Mapping[str, str]) -> None:
    print(f"configured machine client: {config_path}")
    print(f"control_url={config['control_url']}")


if __name__ == "__main__":
    raise SystemExit(main())
