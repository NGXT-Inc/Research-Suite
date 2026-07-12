"""Pull selected sandbox outputs into the local experiment folder."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..utils import ValidationError
from .repo_paths import repo_relative_path, resolve_repo_path


DEFAULT_OUTPUT_PATHS = (
    "results/",
    "figures/",
    "report.md",
    "graph.json",
    "metrics.json",
    "results.json",
)
SSH_CONNECT_TIMEOUT_SECONDS = 15
RSYNC_TIMEOUT_SECONDS = 600

ProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def pull_sandbox_outputs(
    *,
    repo_root: Path,
    sandbox: dict[str, Any],
    paths: list[str] | None = None,
    destination_path: str = "",
    overwrite: bool = False,
    runner: ProcessRunner | None = None,
) -> dict[str, Any]:
    """Copy explicit sandbox paths back over SSH with rsync.

    With no paths, the helper checks for a conservative default set of common
    retained artifacts under the sandbox's experiment directory.
    """
    repo_root = Path(repo_root).resolve()
    runner = runner or subprocess.run
    ssh = sandbox.get("ssh") if isinstance(sandbox.get("ssh"), dict) else {}
    status = str(sandbox.get("status") or "")
    if status != "running":
        raise ValidationError("sandbox.pull_outputs requires a running sandbox")
    remote_dir = str(sandbox.get("experiment_dir") or sandbox.get("workdir") or "")
    if not remote_dir:
        raise ValidationError("sandbox response has no remote experiment_dir")
    ssh_host = str(ssh.get("host") or "")
    try:
        ssh_port = int(ssh.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("sandbox SSH details are incomplete") from exc
    ssh_user = str(ssh.get("user") or "root")
    key_path = str(ssh.get("key_path") or "")
    if not ssh_host or not ssh_port or not key_path:
        raise ValidationError("sandbox SSH details are incomplete")

    destination = _destination_root(
        repo_root=repo_root,
        sandbox=sandbox,
        destination_path=destination_path,
    )
    requested = _normalize_paths(paths or [])
    defaulted = not requested
    if defaulted:
        requested = list(DEFAULT_OUTPUT_PATHS)
        requested = _remote_existing_paths(
            runner=runner,
            remote_dir=remote_dir,
            paths=requested,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            key_path=key_path,
        )

    copied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for rel_path in requested:
        try:
            copied.append(
                _rsync_one(
                    runner=runner,
                    repo_root=repo_root,
                    destination=destination,
                    remote_dir=remote_dir,
                    rel_path=rel_path,
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    key_path=key_path,
                    overwrite=overwrite,
                )
            )
        except ValidationError as exc:
            # One failing path must not discard (or hide) the paths that
            # already landed — report per-path and keep going.
            errors.append({"path": rel_path, "error": str(exc)})
    kept_stale = [
        name for item in copied for name in item.get("files_kept_stale", [])
    ]
    return {
        "ok": not errors,
        "experiment_id": sandbox.get("experiment_id"),
        "sandbox_uid": sandbox.get("sandbox_uid"),
        "sandbox_id": sandbox.get("sandbox_id"),
        "defaulted": defaulted,
        "source_experiment_dir": remote_dir,
        "destination_path": _repo_relative_or_absolute(
            repo_root=repo_root, path=destination
        ),
        "paths_requested": requested,
        "paths_pulled": [item["remote_path"] for item in copied],
        "paths_failed": [item["path"] for item in errors],
        "errors": errors,
        "copied": copied,
        "files_transferred": sum(int(item["files_transferred"]) for item in copied),
        # Local files now present under the pulled paths — including ones that
        # already existed and were deliberately kept (see files_kept_stale).
        "files_present": sum(int(item["files_present"]) for item in copied),
        "bytes_present": sum(int(item["bytes_present"]) for item in copied),
        # Existing local files that differ from the sandbox and were kept
        # because overwrite=false. Re-pull with overwrite=true to replace.
        "files_kept_stale": kept_stale,
    }


def _destination_root(
    *, repo_root: Path, sandbox: dict[str, Any], destination_path: str
) -> Path:
    if destination_path:
        _rel, destination = resolve_repo_path(
            repo_root=repo_root,
            path=destination_path,
            subject="destination_path",
        )
        return destination
    local_dir = str(sandbox.get("local_experiment_dir") or "")
    if local_dir:
        candidate = Path(local_dir).expanduser().resolve()
        try:
            candidate.relative_to(repo_root)
        except ValueError as exc:
            raise ValidationError("sandbox local_experiment_dir escapes repo root") from exc
        return candidate
    experiment_id = str(sandbox.get("experiment_id") or sandbox.get("sandbox_uid") or "")
    if not experiment_id:
        raise ValidationError("experiment_id or destination_path is required")
    return repo_root / "experiments" / experiment_id


def _normalize_paths(paths: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        had_slash = str(raw).endswith("/")
        rel = repo_relative_path(path=str(raw).rstrip("/"), subject="paths[]")
        if not rel:
            raise ValidationError("paths[] may not be empty")
        value = f"{rel}/" if had_slash else rel
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _remote_existing_paths(
    *,
    runner: ProcessRunner,
    remote_dir: str,
    paths: list[str],
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    key_path: str,
) -> list[str]:
    script = (
        f"base={shlex.quote(remote_dir.rstrip('/'))}; "
        + " ".join(
            (
                "p="
                + shlex.quote(path)
                + '; if [ -e "$base/${p%/}" ]; then printf "%s\\n" "$p"; fi;'
            )
            for path in paths
        )
    )
    result = runner(
        _ssh_command(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            key_path=key_path,
            remote_command=script,
        ),
        text=True,
        # Remote-controlled bytes: never let a stray non-UTF8 byte (or an
        # ASCII locale) turn the whole pull into a UnicodeDecodeError.
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=SSH_CONNECT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ValidationError(
            "could not inspect sandbox output paths over SSH",
            details={"stderr": _stderr(result)},
        )
    existing = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    allowed = set(paths)
    return [path for path in existing if path in allowed]


def _rsync_one(
    *,
    runner: ProcessRunner,
    repo_root: Path,
    destination: Path,
    remote_dir: str,
    rel_path: str,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    key_path: str,
    overwrite: bool,
) -> dict[str, Any]:
    is_dir_hint = rel_path.endswith("/")
    clean_rel = rel_path.rstrip("/")
    local_target = destination / clean_rel
    if is_dir_hint:
        local_target.mkdir(parents=True, exist_ok=True)
        destination_arg = f"{local_target}/"
        remote_path = f"{remote_dir.rstrip('/')}/{clean_rel}/"
    else:
        local_target.parent.mkdir(parents=True, exist_ok=True)
        destination_arg = f"{local_target.parent}/"
        remote_path = f"{remote_dir.rstrip('/')}/{clean_rel}"

    def run_rsync(*, dry_run: bool, ignore_existing: bool):
        command = [
            "rsync",
            "-az",
            "--itemize-changes",
            # Sandbox content is semi-trusted (the experiment code wrote it):
            # never recreate its symlinks or device nodes inside the repo.
            "--no-links",
            "--no-devices",
            "--no-specials",
            *(["--dry-run"] if dry_run else []),
            *(["--ignore-existing"] if ignore_existing else []),
            "-e",
            _ssh_transport(ssh_port=ssh_port, key_path=key_path),
            f"{ssh_user}@{ssh_host}:{shlex.quote(remote_path)}",
            destination_arg,
        ]
        result = runner(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RSYNC_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ValidationError(
                f"rsync from sandbox failed for {rel_path}",
                details={"stderr": _stderr(result), "path": rel_path},
            )
        return _itemized_files(result.stdout or "")

    # Without overwrite, a dry run WITHOUT --ignore-existing lists every file
    # that differs from the sandbox; subtracting what the real run actually
    # transferred names the existing local files that were kept stale.
    would_change = run_rsync(dry_run=True, ignore_existing=False) if not overwrite else set()
    transferred = run_rsync(dry_run=False, ignore_existing=not overwrite)
    kept_stale = sorted(would_change - transferred)

    files, bytes_present = _local_stats(local_target)
    return {
        "remote_path": rel_path,
        "local_path": _repo_relative_or_absolute(repo_root=repo_root, path=local_target),
        "files_transferred": len(transferred),
        "files_kept_stale": kept_stale,
        "files_present": files,
        "bytes_present": bytes_present,
    }


def _itemized_files(stdout: str) -> set[str]:
    # --itemize-changes emits one line per changed item, e.g.
    # ">f+++++++++ results.json"; the second column is the file when the
    # item type flag (second char) is 'f'.
    files: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) > 1 and parts[0][1] == "f":
            files.add(parts[1])
    return files


def _ssh_command(
    *,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    key_path: str,
    remote_command: str,
) -> list[str]:
    return [
        "ssh",
        "-i",
        key_path,
        "-p",
        str(ssh_port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{ssh_user}@{ssh_host}",
        remote_command,
    ]


def _ssh_transport(*, ssh_port: int, key_path: str) -> str:
    return (
        f"ssh -i {shlex.quote(key_path)} -p {int(ssh_port)} "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )


def _local_stats(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    files = 0
    bytes_present = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            files += 1
            bytes_present += child.stat().st_size
    return files, bytes_present


def _repo_relative_or_absolute(*, repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _stderr(result: "subprocess.CompletedProcess[str]") -> str:
    return (result.stderr or "").strip()[:2000]
