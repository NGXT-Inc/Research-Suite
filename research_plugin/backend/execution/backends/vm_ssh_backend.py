"""Shared management-SSH operations for VM sandbox backends."""

from __future__ import annotations

from typing import Any, Mapping

from ..vm_ssh import (
    SshInputRunner,
    SshRunner,
    read_transcript_via_mgmt_ssh,
    run_parachute_via_mgmt_ssh,
    run_ssh,
    run_ssh_input,
    run_ssh_parachute,
    retarget_via_mgmt_ssh,
    sample_metrics_via_mgmt_ssh,
    sandbox_tokens,
    write_secrets_via_mgmt_ssh,
)
from ...sandbox.sandbox_backend import SandboxBackendBase


class VmSshSandboxBackend(SandboxBackendBase):
    """Common management-channel behavior for provisioned VM backends."""

    def __init__(
        self,
        *,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
        parachute_runner: SshRunner | None = None,
    ) -> None:
        self._ssh_runner = ssh_runner or run_ssh
        self._ssh_input_runner = ssh_input_runner or run_ssh_input
        self._parachute_runner = parachute_runner or ssh_runner or run_ssh_parachute

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
    ) -> str:
        """Tail the rec.sh transcript over the management SSH channel.

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

    def run_parachute(
        self,
        *,
        sandbox_id: str,
        put_url: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Run the pre-installed parachute over the management channel.

        SSHes as the management principal with the management key (``key_path``)
        and executes ``/opt/rp/parachute.sh`` under sudo, so the tar can read
        every file in the experiment dir regardless of which login wrote it.
        Raises BackendUnavailableError on any failure so the reaper's parachute
        branch surfaces it loudly — a lost experiment dir must never fail
        silently (plan risk 9).
        """
        return run_parachute_via_mgmt_ssh(
            ssh_runner=self._parachute_runner,
            sandbox_id=sandbox_id,
            put_url=put_url,
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

        Writes ``/opt/rp/secrets.env`` (sourced by rec.sh) as ``export NAME=...``
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

    def retarget(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        public_key: str,
        workdir: str,
        sandbox_data_dir: str,
        tracking_env: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Re-point a live VM at another experiment over the management channel.

        Authorizes the target user key, rewrites /opt/rp/env, and creates the
        target workdir/data dirs so a reused sandbox serves the new experiment.
        """
        return retarget_via_mgmt_ssh(
            ssh_runner=self._ssh_input_runner,
            sandbox_id=sandbox_id,
            experiment_id=experiment_id,
            public_key=public_key,
            workdir=workdir,
            sandbox_data_dir=sandbox_data_dir or self.config.sandbox_data_dir,
            tracking_env=tracking_env,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            key_path=key_path,
        )


__all__ = [
    "SshInputRunner",
    "SshRunner",
    "VmSshSandboxBackend",
]
