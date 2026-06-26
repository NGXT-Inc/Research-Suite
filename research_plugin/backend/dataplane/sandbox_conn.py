"""Sandbox SSH key + dispatcher + connection-file plumbing.

``SandboxConnFiles`` owns everything about how the agent reaches a sandbox over
SSH on the local filesystem: the ed25519 keypair, the static ``sbx`` dispatcher
script, and the ``conn`` files the dispatcher sources. It is pure
filesystem/subprocess work with no sandbox-state knowledge, split out of
``SandboxService`` so that machinery is independently testable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..sandbox.sandbox_support import SBX_DISPATCHER, _safe_name, _shq
from ..ssh_keys import ensure_ed25519_keypair


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
        return (
            ensure_ed25519_keypair(
                key_path=key_path,
                comment=f"research-plugin-{experiment_id}",
                missing_action="provision sandbox SSH access",
                failure_subject="sandbox SSH key",
            ),
            key_path,
        )

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

    def write_command_wrapper(
        self,
        *,
        row: dict[str, Any],
        key_path: Path,
        use_sandbox_uid_command: bool = False,
    ) -> str:
        """Refresh the sandbox conn file and return the short command.

        Returns `.research_plugin/sbx <key>` (relative to the repo root).
        Returns "" if the wrapper could not be written, so the caller falls
        back to the raw ssh line.
        """
        dispatcher, conn_dir = self.command_paths()
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        safe_uid = _safe_name(sandbox_uid or experiment_id)
        safe_exp = _safe_name(experiment_id) if experiment_id else ""
        try:
            self.ensure_dispatcher(dispatcher=dispatcher)
            conn_dir.mkdir(parents=True, exist_ok=True)
            body = (
                f"RP_SSH_KEY={_shq(str(key_path))}\n"
                f"RP_SSH_HOST={_shq(str(row.get('ssh_host') or ''))}\n"
                f"RP_SSH_PORT={_shq(str(row.get('ssh_port') or ''))}\n"
                f"RP_SSH_USER={_shq(str(row.get('ssh_user') or 'root'))}\n"
            )
            conn_file = conn_dir / safe_uid
            conn_file.write_text(body)
            os.chmod(conn_file, 0o600)
            if experiment_id and safe_exp and not use_sandbox_uid_command:
                # The experiment alias preserves the single-sandbox command.
                alias = conn_dir / safe_exp
                alias.write_text(body)
                os.chmod(alias, 0o600)
        except OSError:
            return ""
        rel = os.path.relpath(dispatcher, self.repo_root)
        return f"{rel} {safe_uid if use_sandbox_uid_command else safe_exp or safe_uid}"

    def remove_conn(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str = "",
        remove_experiment_alias: bool = True,
    ) -> None:
        """Drop the conn file so `sbx` fails loudly for a dead sandbox."""
        _, conn_dir = self.command_paths()
        try:
            if sandbox_uid:
                (conn_dir / _safe_name(sandbox_uid)).unlink(missing_ok=True)
            if remove_experiment_alias or not sandbox_uid:
                (conn_dir / _safe_name(experiment_id)).unlink(missing_ok=True)
        except OSError:
            pass
