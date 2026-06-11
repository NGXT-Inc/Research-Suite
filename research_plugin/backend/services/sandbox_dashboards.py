"""Dashboard exposure for sandboxes: SSH tunnel pool + MLflow deep links.

`DashboardTunnels` owns the daemon-side ssh ``-L`` port-forward processes that
surface in-sandbox dashboards (MLflow, TensorBoard) for providers without a
native tunnel surface (Lambda Labs), refreshes provider-native URLs (Modal),
and decorates MLflow URLs with a deep link into the newest real run. It talks
to the rest of the registry only through row dicts and `SandboxRegistry`
(persist + events); it never touches the experiments table or the backend's
lifecycle methods beyond the optional dashboard probes.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ..execution import SandboxBackend
from .sandbox_registry import SandboxRegistry
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    decode_dashboards,
    encode_dashboards,
)


@dataclass
class _DashboardTunnel:
    """A daemon-owned SSH local port-forward for one dashboard."""

    process: subprocess.Popen[Any]
    local_port: int
    url: str


class DashboardTunnels:
    """Owns dashboard tunnel processes and dashboard-URL persistence."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        key_path: Callable[..., Path],
    ) -> None:
        self.registry = registry
        self.backend = backend
        self._key_path = key_path
        self._tunnels: dict[tuple[str, str], _DashboardTunnel] = {}
        self._tunnel_attempts: dict[tuple[str, str], float] = {}
        self._tunnels_lock = threading.Lock()
        # (sandbox_id, base_url) -> (computed_at, display_url): TTL cache for
        # the MLflow deep link so polls don't query MLflow every 3 seconds.
        self._mlflow_links: dict[tuple[str, str], tuple[float, str]] = {}

    def maybe_refresh(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Re-read provider-native dashboard URLs and persist if changed.

        Companion to the facade's endpoint refresh: when a sandbox's tunnels
        move on the Modal side, the SSH host/port AND the dashboard HTTPS URLs
        all change together. Best-effort: a backend without ``dashboard_urls``
        or an error reading them leaves the stored value untouched.
        """
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            fresh = self.backend.dashboard_urls(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            return row
        if fresh is None or not isinstance(fresh, dict):
            return row
        normalized = {str(k): str(v) for k, v in fresh.items() if isinstance(v, str) and v}
        encoded = encode_dashboards(normalized)
        if encoded == (row.get("dashboards_json") or "{}"):
            return row
        experiment_id = str(row.get("experiment_id"))
        self.registry.upsert(experiment_id=experiment_id, dashboards_json=encoded)
        return self.registry.load_row(experiment_id=experiment_id)

    def ensure_local(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Expose in-sandbox dashboards through daemon-owned SSH local forwards.

        Modal returns native HTTPS tunnel URLs from the backend. Lambda Labs VMs
        do not have a provider tunnel surface, but they do have SSH. Backends can
        advertise dashboard ports with ``local_dashboard_ports()``; the registry
        then publishes loopback URLs only after the forwarded dashboard responds.
        """
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        sandbox_id = str(row.get("sandbox_id") or "")
        ssh_host = str(row.get("ssh_host") or "")
        key_path = str(
            row.get("key_path")
            or self._key_path(experiment_id=str(row.get("experiment_id") or ""))
        )
        if not sandbox_id or not ssh_host or not key_path:
            return row
        try:
            ports = self.backend.local_dashboard_ports()
        except Exception:  # noqa: BLE001 — dashboard tunnels are best-effort
            return row
        if not isinstance(ports, dict) or not ports:
            return row

        dashboards = decode_dashboards(row.get("dashboards_json"))
        changed = False
        for raw_name, raw_port in ports.items():
            name = str(raw_name)
            try:
                remote_port = int(raw_port)
            except (TypeError, ValueError):
                continue
            if remote_port <= 0:
                continue
            current_url = dashboards.get(name, "")
            # Native provider URLs win. Local tunnels are only the fallback for
            # backends that cannot expose a public dashboard URL themselves.
            if current_url and not _is_local_dashboard_url(current_url):
                continue

            key = (sandbox_id, name)
            tunnel = self._live_tunnel(key=key)
            if tunnel is not None:
                display_url = self._display_url(
                    name=name, base_url=tunnel.url, sandbox_id=sandbox_id
                )
                if dashboards.get(name) != display_url:
                    dashboards[name] = display_url
                    changed = True
                continue

            if current_url:
                dashboards.pop(name, None)
                changed = True
            last_attempt = self._tunnel_attempts.get(key, 0.0)
            if time.monotonic() - last_attempt < 10.0:
                continue
            self._tunnel_attempts[key] = time.monotonic()

            tunnel = self._start_tunnel(
                name=name,
                project_id=str(row.get("project_id") or ""),
                experiment_id=str(row.get("experiment_id") or ""),
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
            dashboards[name] = self._display_url(
                name=name, base_url=tunnel.url, sandbox_id=sandbox_id
            )
            changed = True

        encoded = encode_dashboards(dashboards)
        if not changed and encoded == (row.get("dashboards_json") or "{}"):
            return row
        experiment_id = str(row.get("experiment_id"))
        self.registry.upsert(experiment_id=experiment_id, dashboards_json=encoded)
        return self.registry.load_row(experiment_id=experiment_id)

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

    def _display_url(self, *, name: str, base_url: str, sandbox_id: str) -> str:
        """The URL the UI should embed for one dashboard.

        For MLflow this is a deep link into the most recently active real
        experiment (recomputed at most every 15s, so it upgrades on a later
        poll once training creates the experiment). Everything else uses the
        tunnel URL as-is.
        """
        if name != "mlflow":
            return base_url
        key = (sandbox_id, base_url)
        now = time.monotonic()
        cached = self._mlflow_links.get(key)
        if cached is not None and now - cached[0] < 15.0:
            return cached[1]
        url = _mlflow_deep_link(base_url)
        self._mlflow_links[key] = (now, url)
        return url

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
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.dashboard_tunneled",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": sandbox_id,
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


def _dashboard_url_ready(url: str) -> bool:
    try:
        response = httpx.get(url, timeout=1.0, follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def _mlflow_deep_link(base_url: str) -> str:
    """Deep-link MLflow at the training charts instead of the empty landing page.

    A fresh MLflow opens on the empty "Default" experiment, several clicks away
    from the training charts. The daemon asks MLflow's REST API — through the
    tunnel it owns; the browser cannot, because of CORS — for the newest
    non-Default experiment, then for that experiment's newest run (preferring
    one still RUNNING), and points the iframe straight at the run's
    "Model metrics" tab. Fallbacks, in order: experiment chart view when the
    experiment has no runs yet, the bare URL before any real experiment exists
    or on any error.
    """
    try:
        response = httpx.get(
            f"{base_url}/api/2.0/mlflow/experiments/search",
            params={"max_results": 100},
            timeout=1.5,
        )
        if response.status_code != 200:
            return base_url
        experiments = response.json().get("experiments") or []
    except Exception:  # noqa: BLE001 — the deep link is best-effort sugar
        return base_url
    real = [
        e for e in experiments if isinstance(e, dict) and e.get("name") != "Default"
    ]
    if not real:
        return base_url
    best = max(real, key=lambda e: int(e.get("last_update_time") or 0))
    experiment_id = str(best.get("experiment_id") or "")
    if not experiment_id:
        return base_url
    run_id = _mlflow_latest_run_id(base_url, experiment_id)
    if run_id:
        return f"{base_url}/#/experiments/{experiment_id}/runs/{run_id}/model-metrics"
    return f"{base_url}/#/experiments/{experiment_id}?compareRunsMode=CHART"


def _mlflow_latest_run_id(base_url: str, experiment_id: str) -> str | None:
    """The run worth watching: newest in the experiment, RUNNING beats finished."""
    try:
        response = httpx.post(
            f"{base_url}/api/2.0/mlflow/runs/search",
            json={
                "experiment_ids": [experiment_id],
                "order_by": ["attributes.start_time DESC"],
                "max_results": 20,
            },
            timeout=1.5,
        )
        if response.status_code != 200:
            return None
        runs = response.json().get("runs") or []
    except Exception:  # noqa: BLE001 — best-effort, same as the experiment lookup
        return None
    infos = [
        run.get("info") or {}
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("info"), dict)
    ]
    candidates = [info for info in infos if info.get("run_id")]
    if not candidates:
        return None
    running = [info for info in candidates if info.get("status") == "RUNNING"]
    best = max(running or candidates, key=lambda i: int(i.get("start_time") or 0))
    return str(best["run_id"])


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
