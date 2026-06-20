"""Per-experiment SSH key + dispatcher + connection-file plumbing.

``SandboxConnFiles`` owns everything about how the agent reaches a sandbox over
SSH on the local filesystem: the ed25519 keypair, the static ``sbx`` dispatcher
script, and the per-experiment ``conn`` file the dispatcher sources. It is pure
filesystem/subprocess work with no sandbox-state knowledge, split out of
``SandboxService`` so that machinery is independently testable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ..services.sandbox_support import SBX_DISPATCHER, _safe_name, _shq
from ..utils import ValidationError


class SandboxConnFiles:
    """Manages SSH keys and the repo-local dispatcher/conn files."""

    def __init__(self, *, repo_root: Path, keys_dir: Path) -> None:
        self.repo_root = repo_root
        self.keys_dir = keys_dir

    # ---------- keypair ----------

    def key_path(self, *, experiment_id: str) -> Path:
        return self.keys_dir / _safe_name(experiment_id)

    def ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]:
        key_path = self.key_path(experiment_id=experiment_id)
        pub_path = key_path.with_suffix(".pub")
        if key_path.exists() and pub_path.exists():
            return pub_path.read_text().strip(), key_path
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        # Remove a half-written pair before regenerating.
        for path in (key_path, pub_path):
            if path.exists():
                path.unlink()
        try:
            subprocess.run(
                [
                    "ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-C", f"research-plugin-{experiment_id}",
                    "-f", str(key_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ValidationError(
                "ssh-keygen is required to provision sandbox SSH access but was not found"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ValidationError(
                f"failed to generate sandbox SSH key: {exc.stderr or exc.stdout or exc}"
            ) from exc
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return pub_path.read_text().strip(), key_path

    # ---------- dispatcher / conn files ----------

    def raw_ssh_command(self, *, row: dict[str, Any], key_path: Path) -> str:
        host = row.get("ssh_host") or ""
        port = row.get("ssh_port") or 0
        user = row.get("ssh_user") or "root"
        return (
            f"ssh -i {key_path} -p {port} "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"{user}@{host}"
        )

    def command_paths(self) -> tuple[Path, Path]:
        research_dir = self.repo_root / ".research_plugin"
        return research_dir / "sbx", research_dir / "sandboxes" / "conn"

    def ensure_dispatcher(self, *, dispatcher: Path) -> None:
        """Write the static `sbx` dispatcher once (idempotent)."""
        try:
            if dispatcher.exists() and dispatcher.read_text() == SBX_DISPATCHER:
                return
            dispatcher.parent.mkdir(parents=True, exist_ok=True)
            dispatcher.write_text(SBX_DISPATCHER)
            os.chmod(dispatcher, 0o755)
        except OSError:
            # The dispatcher is a convenience; never fail provisioning over it.
            # The raw_command fallback still works.
            pass

    def write_command_wrapper(self, *, row: dict[str, Any], key_path: Path) -> str:
        """Refresh the per-experiment conn file and return the short command.

        Returns `.research_plugin/sbx <experiment_id>` (relative to the repo
        root). Returns "" if the wrapper could not be written, so the caller
        falls back to the raw ssh line.
        """
        dispatcher, conn_dir = self.command_paths()
        safe = _safe_name(str(row.get("experiment_id") or ""))
        try:
            self.ensure_dispatcher(dispatcher=dispatcher)
            conn_dir.mkdir(parents=True, exist_ok=True)
            conn_file = conn_dir / safe
            conn_file.write_text(
                f"RP_SSH_KEY={_shq(str(key_path))}\n"
                f"RP_SSH_HOST={_shq(str(row.get('ssh_host') or ''))}\n"
                f"RP_SSH_PORT={_shq(str(row.get('ssh_port') or ''))}\n"
                f"RP_SSH_USER={_shq(str(row.get('ssh_user') or 'root'))}\n"
            )
            os.chmod(conn_file, 0o600)
        except OSError:
            return ""
        rel = os.path.relpath(dispatcher, self.repo_root)
        return f"{rel} {safe}"

    def remove_conn(self, *, experiment_id: str) -> None:
        """Drop the conn file so `sbx` fails loudly for a dead sandbox."""
        _, conn_dir = self.command_paths()
        try:
            (conn_dir / _safe_name(experiment_id)).unlink(missing_ok=True)
        except OSError:
            pass
