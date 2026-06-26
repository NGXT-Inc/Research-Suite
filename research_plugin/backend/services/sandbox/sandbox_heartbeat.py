"""Sandbox heartbeat policy plus the control-plane idle monitor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from ...sandbox.sandbox_support import parse_iso
from ...utils import format_iso


class SandboxIdlePolicy:
    """Pure idle decision from two usage samples."""

    max_cpu_cores = 0.01
    max_gpu_util_pct = 1.0
    max_network_bytes_per_second = 1024.0
    max_memory_bytes_per_second = 1024.0 * 1024.0

    def is_idle(
        self,
        *,
        current: dict[str, Any],
        previous: dict[str, Any] | None,
        elapsed_seconds: float,
    ) -> bool:
        if previous is None or elapsed_seconds <= 0:
            return False
        # A live SSH session blocks idle; an UNMEASURABLE one (None — e.g. Modal
        # has no sshd, or ss/proc are absent) must not, or such boxes could never
        # reap. The activity signals below still guard genuinely-busy work.
        ssh = self._ssh_established(current)
        if ssh is not None and ssh != 0:
            return False
        cpu = _float((current.get("cpu") or {}).get("used_cores"))
        if cpu is None or cpu > self.max_cpu_cores:
            return False
        gpus = current.get("gpus") or []
        if not isinstance(gpus, list):
            return False
        for gpu in gpus:
            util = (
                _float((gpu or {}).get("util_pct")) if isinstance(gpu, dict) else None
            )
            if util is None or util > self.max_gpu_util_pct:
                return False
        net_rate = _rate(
            _network_bytes(current),
            _network_bytes(previous),
            elapsed_seconds,
            absolute=False,
        )
        if net_rate is None or net_rate > self.max_network_bytes_per_second:
            return False
        mem_rate = _rate(
            _memory_bytes(current),
            _memory_bytes(previous),
            elapsed_seconds,
            absolute=True,
        )
        return mem_rate is not None and mem_rate <= self.max_memory_bytes_per_second

    def next_idle_since(
        self,
        *,
        idle_since: datetime | None,
        now: datetime,
        is_idle: bool,
    ) -> datetime | None:
        return (idle_since or now) if is_idle else None

    def should_reap(
        self,
        *,
        idle_since: datetime | None,
        now: datetime,
        threshold_seconds: float,
    ) -> bool:
        return (
            threshold_seconds > 0
            and idle_since is not None
            and (now - idle_since).total_seconds() >= threshold_seconds
        )

    def _ssh_established(self, sample: dict[str, Any]) -> int | None:
        return _int((sample.get("network") or {}).get("ssh_established"))


class SandboxHeartbeatMonitor:
    """Samples running sandboxes and delegates reap decisions to the policy."""

    def __init__(
        self,
        *,
        registry: Any,
        sample_metrics: Callable[..., dict[str, Any]],
        reap_row: Callable[..., None],
        policy: SandboxIdlePolicy | None = None,
    ) -> None:
        self.registry = registry
        self.sample_metrics = sample_metrics
        self.reap_row = reap_row
        self.policy = policy or SandboxIdlePolicy()

    def reap_idle(
        self, *, now: datetime | None = None, threshold_seconds: float
    ) -> int:
        if threshold_seconds <= 0:
            return 0
        now_dt = now or datetime.now(tz=UTC)
        reaped = 0
        for row in self.registry.list_running_rows():
            try:
                if self._tick_row(
                    row=row, now=now_dt, threshold_seconds=threshold_seconds
                ):
                    reaped += 1
            except Exception:  # noqa: BLE001 - heartbeat must never kill the loop
                continue
        return reaped

    def _tick_row(
        self, *, row: dict[str, Any], now: datetime, threshold_seconds: float
    ) -> bool:
        experiment_id = str(row.get("experiment_id") or "")
        if not experiment_id:
            return False
        metrics = self._sample(row=row, experiment_id=experiment_id)
        if not isinstance(metrics, dict):
            return False
        previous_record = self.registry.heartbeat_snapshot(row=row)
        previous = (
            previous_record.get("metrics")
            if isinstance(previous_record, dict)
            else None
        )
        previous_at = parse_iso(
            previous_record.get("sampled_at")
            if isinstance(previous_record, dict)
            else None
        )
        idle_since = parse_iso(row.get("idle_since"))
        if not isinstance(previous, dict) or previous_at is None:
            self.registry.record_heartbeat(
                experiment_id=experiment_id,
                sandbox_uid=str(row.get("sandbox_uid") or ""),
                idle_since=None,
                snapshot=self._snapshot(metrics=metrics, now=now),
            )
            return False
        is_idle = self.policy.is_idle(
            current=metrics,
            previous=previous,
            elapsed_seconds=(now - previous_at).total_seconds(),
        )
        next_idle_since = self.policy.next_idle_since(
            idle_since=idle_since, now=now, is_idle=is_idle
        )
        self.registry.record_heartbeat(
            experiment_id=experiment_id,
            sandbox_uid=str(row.get("sandbox_uid") or ""),
            idle_since=format_iso(next_idle_since) if next_idle_since else None,
            snapshot=self._snapshot(metrics=metrics, now=now),
        )
        if not self.policy.should_reap(
            idle_since=next_idle_since,
            now=now,
            threshold_seconds=threshold_seconds,
        ):
            return False
        self.reap_row(
            row=row,
            event_type="sandbox.idle_reaped",
            transition_reason="sandbox reaped after idle threshold",
            payload_extra={
                "idle_since": format_iso(next_idle_since),
                "idle_seconds": int((now - next_idle_since).total_seconds()),
                "threshold_seconds": int(threshold_seconds),
            },
        )
        return True

    def _sample(
        self, *, row: dict[str, Any], experiment_id: str
    ) -> dict[str, Any] | None:
        result = self.sample_metrics(
            experiment_id=experiment_id,
            project_id=str(row.get("project_id") or ""),
            sandbox_uid=str(row.get("sandbox_uid") or ""),
        )
        metrics = result.get("metrics") if isinstance(result, dict) else None
        return metrics if isinstance(metrics, dict) else None

    def _snapshot(self, *, metrics: dict[str, Any], now: datetime) -> dict[str, Any]:
        return {"sampled_at": format_iso(now), "metrics": metrics}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _memory_bytes(sample: dict[str, Any]) -> int | None:
    return _int((sample.get("memory") or {}).get("used_bytes"))


def _network_bytes(sample: dict[str, Any]) -> int | None:
    return _int((sample.get("network") or {}).get("bytes_total"))


def _rate(
    current: int | None,
    previous: int | None,
    elapsed_seconds: float,
    *,
    absolute: bool,
) -> float | None:
    if current is None or previous is None or elapsed_seconds <= 0:
        return None
    delta = current - previous
    if not absolute and delta < 0:
        return None
    return abs(delta if absolute else max(delta, 0)) / elapsed_seconds
