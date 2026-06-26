"""Dashboard exposure for sandbox-local TensorBoard.

`DashboardTunnels` owns the daemon-side ssh ``-L`` port-forward processes that
surface sandbox-local dashboards (TensorBoard for new runs) for providers
without a native tunnel surface (Lambda Labs). Loopback URLs are machine-local
facts (cloud plan §3.2): they persist in the data-plane worker's local store,
never in the cloud-bound sandbox row, and views merge them back through
``merged_row``. The provider-native URL refresh lives in the sandbox facade.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from ..sandbox.sandbox_backend import SandboxBackend
from ..sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    decode_dashboards,
    encode_dashboards,
)


class LocalDashboardStore(Protocol):
    """The slice of the worker's local state the tunnel pool needs."""

    def dashboards_local(self, *, experiment_id: str) -> dict[str, str]: ...

    def record(
        self,
        *,
        experiment_id: str,
        key_path: str | None = None,
        local_sync_dir: str | None = None,
        dashboards_local: dict[str, str] | None = None,
    ) -> None: ...


@dataclass
class _DashboardTunnel:
    """A daemon-owned SSH local port-forward for one dashboard."""

    process: subprocess.Popen[Any]
    local_port: int
    url: str


class DashboardTunnels:
    """Owns dashboard tunnel processes and loopback-URL persistence."""

    def __init__(
        self,
        *,
        backend: SandboxBackend,
        key_path: Callable[..., Path],
        local_state: LocalDashboardStore,
        emit_event: Callable[..., None] | None = None,
    ) -> None:
        self.backend = backend
        self._key_path = key_path
        self.local_state = local_state
        # Record sink for 'sandbox.dashboard_tunneled'; bound late by the
        # facade (registry.emit_event) because events are control-plane rows.
        self.emit_event = emit_event
        self._tunnels: dict[tuple[str, str], _DashboardTunnel] = {}
        self._tunnel_attempts: dict[tuple[str, str], float] = {}
        self._tunnels_lock = threading.Lock()
    def merged_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """The row with provider URLs + locally stored loopback URLs merged.

        Loopback URLs never live in the row; rows written before the split may
        still carry them, so the row's map is filtered to provider-portable
        URLs before the worker-local map overlays it.
        """
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = experiment_id or sandbox_uid
        provider = _provider_dashboards(row=row)
        local = self.local_state.dashboards_local(experiment_id=local_key)
        merged = encode_dashboards({**provider, **local})
        if merged == (row.get("dashboards_json") or "{}"):
            return row
        out = dict(row)
        out["dashboards_json"] = merged
        return out

    def ensure_local(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Expose in-sandbox dashboards through daemon-owned SSH local forwards.

        Modal returns native HTTPS tunnel URLs from the backend. Lambda Labs VMs
        do not have a provider tunnel surface, but they do have SSH. Backends can
        advertise dashboard ports with ``local_dashboard_ports()``; loopback
        URLs are published only after the forwarded dashboard responds.
        """
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return self.merged_row(row=row)
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = experiment_id or sandbox_uid
        sandbox_id = str(row.get("sandbox_id") or "")
        ssh_host = str(row.get("ssh_host") or "")
        key_path = str(self._key_path(experiment_id=local_key))
        if not sandbox_id or not ssh_host or not key_path:
            return self.merged_row(row=row)
        try:
            ports = self.backend.local_dashboard_ports()
        except Exception:  # noqa: BLE001 — dashboard tunnels are best-effort
            return self.merged_row(row=row)
        if not isinstance(ports, dict) or not ports:
            return self.merged_row(row=row)

        provider = _provider_dashboards(row=row)
        local = self.local_state.dashboards_local(experiment_id=local_key)
        changed = False
        for raw_name, raw_port in ports.items():
            name = str(raw_name)
            try:
                remote_port = int(raw_port)
            except (TypeError, ValueError):
                continue
            if remote_port <= 0:
                continue
            # Native provider URLs win. Local tunnels are only the fallback for
            # backends that cannot expose a public dashboard URL themselves.
            if provider.get(name):
                continue

            key = (sandbox_id, name)
            tunnel = self._live_tunnel(key=key)
            if tunnel is not None:
                if local.get(name) != tunnel.url:
                    local[name] = tunnel.url
                    changed = True
                continue

            if local.get(name):
                local.pop(name, None)
                changed = True
            last_attempt = self._tunnel_attempts.get(key, 0.0)
            if time.monotonic() - last_attempt < 10.0:
                continue
            self._tunnel_attempts[key] = time.monotonic()

            tunnel = self._start_tunnel(
                name=name,
                project_id=str(row.get("project_id") or ""),
                experiment_id=experiment_id,
                sandbox_uid=sandbox_uid,
                sandbox_id=sandbox_id,
                ssh_host=ssh_host,
                ssh_port=int(row.get("ssh_port") or 22),
                ssh_user=str(row.get("ssh_user") or "root"),
                key_path=key_path,
                remote_port=remote_port,
            )
            if tunnel is None:
                continue
            with self._tunnels_lock:
                self._tunnels[key] = tunnel
            local[name] = tunnel.url
            changed = True

        if changed:
            self.local_state.record(
                experiment_id=local_key, dashboards_local=local
            )
        merged = encode_dashboards({**provider, **local})
        if merged == (row.get("dashboards_json") or "{}"):
            return row
        out = dict(row)
        out["dashboards_json"] = merged
        return out

    def stop(self, *, sandbox_id: str = "") -> None:
        """Tear down tunnels for one sandbox, or every tunnel when id is ''."""
        with self._tunnels_lock:
            if sandbox_id:
                items = [
                    (key, tunnel)
                    for key, tunnel in self._tunnels.items()
                    if key[0] == sandbox_id
                ]
            else:
                items = list(self._tunnels.items())
            for key, _ in items:
                self._tunnels.pop(key, None)
                self._tunnel_attempts.pop(key, None)
        for _, tunnel in items:
            self._terminate_process(tunnel.process)

    # ---------- internals ----------

    def _live_tunnel(self, *, key: tuple[str, str]) -> _DashboardTunnel | None:
        with self._tunnels_lock:
            tunnel = self._tunnels.get(key)
            if tunnel is None:
                return None
            if tunnel.process.poll() is None:
                return tunnel
            self._tunnels.pop(key, None)
        self._terminate_process(tunnel.process)
        return None

    def _start_tunnel(
        self,
        *,
        name: str,
        project_id: str,
        experiment_id: str,
        sandbox_uid: str,
        sandbox_id: str,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: str,
        remote_port: int,
    ) -> _DashboardTunnel | None:
        local_port = _free_local_port()
        command = [
            "ssh",
            "-N",
            "-i", key_path,
            "-p", str(int(ssh_port) or 22),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=5",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            f"{ssh_user}@{ssh_host}",
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return None
        tunnel = _DashboardTunnel(
            process=proc,
            local_port=local_port,
            url=f"http://127.0.0.1:{local_port}",
        )
        if not _tunnel_ready(proc, local_port, tunnel.url):
            self._terminate_process(proc)
            return None
        if self.emit_event is not None:
            self.emit_event(
                project_id=project_id,
                event_type="sandbox.dashboard_tunneled",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": sandbox_id,
                    "sandbox_uid": sandbox_uid,
                    "dashboard": name,
                    "local_port": local_port,
                    "remote_port": remote_port,
                },
            )
        return tunnel

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[Any]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_local_dashboard_url(url: str) -> bool:
    return url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")


def _provider_dashboards(*, row: dict[str, Any]) -> dict[str, str]:
    """Provider-portable URLs from the row; loopback entries (legacy rows
    written before the split) are dropped — the worker store owns those."""
    return {
        name: url
        for name, url in decode_dashboards(row.get("dashboards_json")).items()
        if not _is_local_dashboard_url(url)
    }


def _dashboard_url_ready(url: str) -> bool:
    try:
        response = httpx.get(url, timeout=1.0, follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def _tunnel_ready(
    proc: subprocess.Popen[Any], local_port: int, url: str, *, timeout: float = 6.0
) -> bool:
    """True once the ssh -L listener is bound AND the dashboard answers through it.

    A cold ssh handshake takes 0.5-2s before the local forward port exists, so
    a single instant probe always loses the race (this is why Lambda dashboard
    tabs never surfaced even with the servers running). Wait for the local bind
    first — that only proves ssh connected — then make one end-to-end HTTP
    probe through the forward, which is what actually tests the remote service.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            return False  # ssh died: auth failure or ExitOnForwardFailure
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                break
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
    return _dashboard_url_ready(url)
