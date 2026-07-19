"""Machine-level CLI for hosted-control + local-data split mode."""

from __future__ import annotations

from contextlib import closing, suppress
import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from merv.shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    CONTROL_URL_ENV_VAR,
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
        print(f"merv-client: {exc}", file=sys.stderr)
        return 2


class ClientError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merv-client",
        description="Configure hosted control and link local folders to Merv projects.",
    )
    parser.add_argument(
        "--config",
        help=(
            "Machine client config path (default: ~/.merv/client.json, or the "
            "legacy ~/.research_plugin/client.json when that dir exists)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser(
        "configure",
        help="Save hosted-control URL for this machine.",
    )
    _add_control_args(configure)
    configure.set_defaults(func=_cmd_configure)

    login = sub.add_parser(
        "login",
        help=(
            "Sign in to the hosted brain: opens the browser (Google or "
            "email/password via your RapidReview account) and stores the "
            "session on this machine. Use --api-key for headless setups."
        ),
    )
    _add_control_args(login)
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the login URL instead of opening a browser (SSH/containers).",
    )
    login.set_defaults(func=_cmd_login)

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
    _add_control_args(connect)
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


def _add_control_args(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument(
        "--api-key",
        required=False,
        default=None,
        help=(
            "RapidReview API key (rr_sk_...) for the hosted brain; mint one in "
            "the RapidReview account settings. Stored 0600 in the client "
            "config. Not needed for a local deployment."
        ),
    )


def _cmd_configure(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    config = configure_client(
        config_path=config_path,
        control_url=args.control_url or HOSTED_CONTROL_URL,
        api_key=args.api_key,
    )
    _print_configured(config_path=config_path, config=config)
    return 0


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        with suppress(Exception):
            body = json.loads(exc.read().decode("utf-8")).get("detail", "")
        raise ClientError(body or f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"brain unreachable at {url}: {exc.reason}") from exc


def _cmd_login(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    existing = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    control_url = (
        args.control_url or existing.get("control_url", "") or HOSTED_CONTROL_URL
    ).rstrip("/")
    # Headless shortcut: a RapidReview API key needs no browser at all.
    if args.api_key:
        config = configure_client(
            config_path=config_path, control_url=control_url, api_key=args.api_key
        )
        _print_configured(config_path=config_path, config=config)
        return 0

    data = _post_json(f"{control_url}/api/sdk/auth/session", {})
    auth_url = data["auth_url"]
    print(f"Login URL: {auth_url}")
    opened = False
    if not args.no_browser:
        try:
            opened = webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            opened = False
    print(
        "(Browser window should have opened)"
        if opened
        else "Open the URL above in your browser to log in."
    )
    print("Waiting for sign-in... (Ctrl+C to cancel)")
    try:
        for _ in range(150):  # 5 minutes at 2s intervals
            time.sleep(2)
            result = _post_json(
                f"{control_url}/api/sdk/auth/session/poll",
                {"session_id": data["session_id"]},
            )
            if result.get("status") == "complete":
                config = configure_client(
                    config_path=config_path,
                    control_url=control_url,
                    session={
                        "access_token": result.get("access_token", ""),
                        "refresh_token": result.get("refresh_token", ""),
                        "expires_at": int(time.time()) + int(result.get("expires_in") or 3600),
                        "email": result.get("email", ""),
                    },
                )
                print(f"Logged in as {config.get('email') or 'your account'}")
                _print_configured(config_path=config_path, config=config)
                return 0
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        return 1
    raise ClientError("login timed out; rerun merv-client login")


def _cmd_connect(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    existing = read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})
    if _has_control_config_args(args):
        control_url = args.control_url or existing.get("control_url", "")
        config = configure_client(
            config_path=config_path,
            control_url=control_url or HOSTED_CONTROL_URL,
            api_key=args.api_key,
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
        "MERV_REPO_ROOT": str(repo),
        CONTROL_URL_ENV_VAR: config["control_url"],
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
    api_key: str | None = None,
    session: Mapping[str, Any] | None = None,
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
    # None preserves an already-stored credential; a provided value replaces it.
    stored_key = existing.get("api_key", "") if api_key is None else api_key.strip()
    if stored_key:
        config["api_key"] = stored_key
    session_fields = existing if session is None else session
    # Preserve a stored browser-login session unless a replacement was supplied.
    for field in ("access_token", "refresh_token", "expires_at", "email"):
        value = session_fields.get(field)
        if value:
            config[field] = str(value)
    _write_json_private(config_path, config)
    return config


def link_repo(*, config_path: Path, repo_root: Path, project_id: str) -> dict[str, Any]:
    if not project_id:
        raise ClientError("project_id is required")
    links = _project_links(config_path=config_path)
    canonical = str(repo_root.expanduser().resolve())
    with closing(_links_connect(links)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO project_links (repo_root, project_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(repo_root) DO UPDATE SET "
                "project_id = excluded.project_id",
                (canonical, project_id, _now_iso()),
            )
    return {"linked": True, "repo_root": canonical, "project_id": project_id}


def route_repo(*, config_path: Path, repo_root: Path) -> dict[str, Any]:
    links = _project_links(config_path=config_path)
    canonical = str(repo_root.expanduser().resolve())
    with closing(_links_connect(links)) as conn:
        row = conn.execute(
            "SELECT project_id FROM project_links WHERE repo_root = ?",
            (canonical,),
        ).fetchone()
    project_id = str(row["project_id"]) if row is not None else None
    return {"exists": bool(project_id), "repo_root": canonical, "project_id": project_id}


def list_links(*, config_path: Path) -> dict[str, Any]:
    with closing(_links_connect(_project_links(config_path=config_path))) as conn:
        rows = conn.execute(
            "SELECT repo_root, project_id, created_at FROM project_links ORDER BY repo_root"
        ).fetchall()
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
    with closing(_links_connect(_project_links(config_path=config_path))) as conn:
        with conn:
            cur = conn.execute(
                "DELETE FROM project_links WHERE repo_root = ?", (canonical,)
            )
    return {"unlinked": bool(cur.rowcount), "repo_root": canonical}


def _config_path(args: argparse.Namespace) -> Path:
    if getattr(args, "config", None):
        return Path(args.config).expanduser().resolve()
    return resolve_client_config_path()


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
    return bool(getattr(args, "control_url", None)) or getattr(args, "api_key", None) is not None


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
    # Never echo credentials themselves — this output gets pasted into issues/docs.
    print(f"api_key={'set' if config.get('api_key') else 'unset'}")
    print(f"session={'signed in' if config.get('access_token') else 'none'}")


if __name__ == "__main__":
    raise SystemExit(main())
