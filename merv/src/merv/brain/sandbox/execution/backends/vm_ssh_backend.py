"""Shared management-SSH operations for VM sandbox backends."""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Mapping

from ..vm_ssh import (
    SshInputRunner,
    SshRunner,
    read_runs_via_mgmt_ssh,
    read_transcript_via_mgmt_ssh,
    run_ssh,
    run_ssh_input,
    sample_metrics_via_mgmt_ssh,
    sandbox_tokens,
    write_secrets_via_mgmt_ssh,
)
from ...sandbox_backend import BackendUnavailableError, SandboxBackendBase, TranscriptTail


class VmSshSandboxBackend(SandboxBackendBase):
    """Common management-channel behavior for provisioned VM backends."""

    def __init__(
        self,
        *,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        self._ssh_runner = ssh_runner or run_ssh
        self._ssh_input_runner = ssh_input_runner or run_ssh_input

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,  # noqa: ARG002 — VM backends have no volume
        workdir: str,
        tail: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",  # noqa: ARG002 — the management channel uses its own principal, not the row data-plane ssh_user
        key_path: str = "",
    ) -> TranscriptTail:
        """Tail the rec.sh transcript over the management SSH channel.

        Returns the tail window plus the transcript's true byte size, so the
        service can keep byte-offset cursors valid past the window.

        ``key_path`` is the per-sandbox management private key (plan Phase 5,
        fixed decision 4); the read logs in as the dedicated management
        principal, which bootstrap exempts from the rec.sh ForceCommand — so
        polling the transcript is never itself recorded as a command and never
        depends on the user's machine. The row's data-plane ``ssh_user`` is
        ignored.
        """
        return read_transcript_via_mgmt_ssh(
            ssh_runner=self._ssh_runner,
            sandbox_id=sandbox_id,
            experiment_id=experiment_id,
            workdir=workdir,
            remote_root=self.config.remote_root,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
            tail=tail,
        )

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",  # noqa: ARG002 — the management channel uses its own principal, not the row data-plane ssh_user
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Sample live VM usage (CPU/RAM/GPU) over the management SSH channel.

        Runs the shared sampler script as the ForceCommand-exempt management
        principal (``key_path`` is the management key, plan Phase 5), so the
        ~3s UI poll never spams the experiment transcript. Returns a parsed
        gauge dict, or None when the VM is unreachable or the sampler produced
        nothing usable; never raises.
        """
        return sample_metrics_via_mgmt_ssh(
            ssh_runner=self._ssh_runner,
            sandbox_id=sandbox_id,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
        )

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",  # noqa: ARG002 — the management channel uses its own principal, not the row data-plane ssh_user
        key_path: str = "",
    ) -> list[dict[str, Any]] | None:
        """List merv_run receipts under workdir/.runs over the management channel.

        Same principal and never-raises contract as sample_metrics: [] means
        the box answered with no runs, None means the box did not answer.
        """
        return read_runs_via_mgmt_ssh(
            ssh_runner=self._ssh_runner,
            sandbox_id=sandbox_id,
            workdir=workdir,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
        )

    def sandbox_secrets(self) -> dict[str, str]:
        """The credentials to deliver to a fresh VM post-boot (HF tokens).

        Resolved from the control plane's env / secret store; the control side
        hands these to write_secrets. Empty when none are configured.
        """
        return sandbox_tokens()

    def sandbox_environment(self) -> dict:
        available_tokens: list[str] = []
        if os.environ.get("HF_TOKEN"):
            available_tokens.append("HF_TOKEN")
        return {
            "available_tokens": available_tokens,
            "notes": (
                [
                    "HF_TOKEN is available inside the sandbox for Hugging Face downloads. "
                    "Do not print or write the token; use it through Hugging Face tooling."
                ]
                if available_tokens
                else []
            ),
        }

    def _wait_for_ssh(self, *, host: str, port: int = 22) -> None:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=10):
                    return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"SSH never became reachable on {host}:{port} ({last_error})"
        )

    def write_secrets(
        self,
        *,
        sandbox_id: str,
        secrets: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Deliver provider credentials post-boot over the management channel.

        Writes ``/opt/merv/secrets.env`` (sourced by rec.sh) as ``export NAME=...``
        lines over SSH stdin, replacing the cleartext-in-user_data embed (plan
        Phase 9, risk 16). Best-effort: returns False on any failure and never
        raises — provisioning must not fail because a token write was flaky.
        """
        return write_secrets_via_mgmt_ssh(
            ssh_runner=self._ssh_input_runner,
            sandbox_id=sandbox_id,
            secrets=secrets,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
        )


__all__ = [
    "SshInputRunner",
    "SshRunner",
    "VmSshSandboxBackend",
]
