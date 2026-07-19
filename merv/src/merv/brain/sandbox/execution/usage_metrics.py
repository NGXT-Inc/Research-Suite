"""Shared live-usage sampler for SSH-accessible compute environments.

Both execution backends run the same read-only probe script and parse the same
``MERV <key>=<value>`` line protocol:

  - Modal executes it via control-plane ``sandbox.exec`` (inside a gVisor
    container, where the cgroup files scope to the container).
  - Lambda Labs executes it over plain SSH through the rec.sh transcript-read
    bypass (on a full VM, where the root cgroup files scope to the whole
    machine — which is exactly the gauge we want for a dedicated VM).

Every probe degrades to silence rather than failing, so a CPU-only machine
(no nvidia-smi) still returns what it can.
"""

from __future__ import annotations

from typing import Any


METRICS_EXEC_TIMEOUT = 15


# Emits machine-parseable `MERV <key>=<value>` lines for the usage gauges:
# CPU cores in use (a two-point cgroup delta), memory in use (anonymous RSS
# from /proc/meminfo — the reclaimable page cache a memory-mapped dataset
# inflates "used" with is deliberately excluded, and under gVisor the
# cgroup/meminfo limits are host-level and unusable as denominators, so no
# limit is emitted from these files), cumulative network bytes + SSH sessions,
# and per-GPU utilization + VRAM via nvidia-smi.
METRICS_SCRIPT = r"""
set -u
now_ns() { date +%s%N; }
# Cumulative CPU time in microseconds (cgroup v2 usage_usec, else v1 cpuacct in
# ns / 1000). NOTE: the sandbox's awk is mawk, whose printf "%d" is 32-bit and
# silently clamps at INT_MAX (2147483647) — a cumulative counter blows past that
# in well under an hour, which would clamp BOTH samples to the same value and
# report 0 cores. Use %.0f (double-backed) for the conversion to stay exact.
cpu_usage_usec() {
  if [ -r /sys/fs/cgroup/cpu.stat ]; then
    awk '/^usage_usec/{print $2; exit}' /sys/fs/cgroup/cpu.stat
  elif [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then
    awk '{printf "%.0f", $1/1000}' /sys/fs/cgroup/cpuacct/cpuacct.usage
  fi
}
u1=$(cpu_usage_usec); t1=$(now_ns)
sleep 0.25
u2=$(cpu_usage_usec); t2=$(now_ns)
if [ -n "${u1:-}" ] && [ -n "${u2:-}" ]; then
  awk -v a="$u1" -v b="$u2" -v ta="$t1" -v tb="$t2" \
    'BEGIN{ d=tb-ta; if(d>0) printf "MERV cpu_cores_used=%.4f\n", ((b-a)*1000.0)/d }'
fi
if [ -r /sys/fs/cgroup/cpu.max ]; then
  read -r q p < /sys/fs/cgroup/cpu.max || true
  if [ "${q:-max}" != "max" ] && [ -n "${p:-}" ]; then
    awk -v q="$q" -v p="$p" 'BEGIN{ if(p>0) printf "MERV cpu_cores_limit=%.4f\n", q/p }'
  fi
fi
# Memory used. Modal runs sandboxes under gVisor, where the per-container memory
# cgroup is NOT projected in (the root cgroup and /proc/meminfo report host-level
# totals), so cgroup usage/limit are useless here. Derive "used" as anonymous +
# unreclaimable memory = MemTotal - MemFree - Buffers - Cached - SReclaimable.
# This deliberately excludes the reclaimable page cache that a memory-mapped
# dataset inflates "used" with (it would otherwise read as ~all of host RAM and
# isn't real memory pressure). The denominator is the reserved request, which the
# backend supplies — we intentionally do NOT emit a limit from these host files.
if [ -r /proc/meminfo ]; then
  awk '
    /^MemTotal:/      {t=$2}
    /^MemFree:/       {f=$2}
    /^Buffers:/       {b=$2}
    /^Cached:/        {c=$2}
    /^SReclaimable:/  {s=$2}
    END { u=t-f-b-c-s; if (u<0) u=0; printf "MERV mem_used_bytes=%.0f\n", u*1024 }
  ' /proc/meminfo
fi
if [ -r /proc/net/dev ]; then
  awk 'NR>2{iface=$1; sub(/:/,"",iface); if(iface!="lo") total+=$2+$10}
       END{printf "MERV net_bytes_total=%.0f\n", total}' /proc/net/dev
fi
ssh_sessions() {
  if command -v ss >/dev/null 2>&1; then
    ss -Htn state established 'sport = :22' 2>/dev/null | wc -l | awk '{print $1}'
    return
  fi
  awk 'NR>1{split($2,a,":"); if(tolower(a[2])=="0016" && $4=="01") c++}
       END{print c+0}' /proc/net/tcp /proc/net/tcp6 2>/dev/null
}
ssh_established=$(ssh_sessions || true)
if [ -n "${ssh_established:-}" ]; then
  printf 'MERV ssh_established=%s\n' "$ssh_established"
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,name \
    --format=csv,noheader,nounits 2>/dev/null | \
  while IFS=',' read -r idx util used total name; do
    trim() { echo "$1" | sed 's/^ *//; s/ *$//'; }
    printf 'MERV gpu idx=%s util=%s used=%s total=%s name=%s\n' \
      "$(trim "$idx")" "$(trim "$util")" "$(trim "$used")" "$(trim "$total")" "$(trim "$name")"
  done
fi
echo "MERV ok=1"
"""


def _to_float(value: str | None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _parse_gpu(body: str) -> dict[str, Any] | None:
    """Parse one `idx=.. util=.. used=.. total=.. name=..` GPU line."""
    name = ""
    head = body
    if " name=" in body:
        head, name = body.split(" name=", 1)
    fields: dict[str, str] = {}
    for token in head.split():
        if "=" in token:
            key, val = token.split("=", 1)
            fields[key] = val
    index = _to_int(fields.get("idx"))
    if index is None:
        return None
    return {
        "index": index,
        "name": name.strip(),
        "util_pct": _to_int(fields.get("util")),
        "mem_used_mib": _to_int(fields.get("used")),
        "mem_total_mib": _to_int(fields.get("total")),
    }


def parse_metrics(output: str) -> dict[str, Any] | None:
    """Turn `MERV key=value` sampler lines into a structured gauge dict."""
    cpu_used = cpu_limit = None
    mem_used = mem_limit = None
    net_bytes = ssh_established = None
    gpus: list[dict[str, Any]] = []
    saw_ok = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("MERV "):
            continue
        body = line[5:]
        if body.startswith("cpu_cores_used="):
            cpu_used = _to_float(body.split("=", 1)[1])
        elif body.startswith("cpu_cores_limit="):
            cpu_limit = _to_float(body.split("=", 1)[1])
        elif body.startswith("mem_used_bytes="):
            mem_used = _to_int(body.split("=", 1)[1])
        elif body.startswith("mem_limit_bytes="):
            mem_limit = _to_int(body.split("=", 1)[1])
        elif body.startswith("net_bytes_total="):
            net_bytes = _to_int(body.split("=", 1)[1])
        elif body.startswith("ssh_established="):
            ssh_established = _to_int(body.split("=", 1)[1])
        elif body.startswith("gpu "):
            gpu = _parse_gpu(body[4:])
            if gpu is not None:
                gpus.append(gpu)
        elif body.startswith("ok="):
            saw_ok = body.split("=", 1)[1].strip() == "1"
    if (
        not saw_ok
        and cpu_used is None
        and mem_used is None
        and net_bytes is None
        and not gpus
    ):
        return None
    return {
        "cpu": {"used_cores": cpu_used, "limit_cores": cpu_limit},
        "memory": {"used_bytes": mem_used, "limit_bytes": mem_limit},
        "network": {
            "bytes_total": net_bytes,
            "ssh_established": ssh_established,
        },
        "gpus": gpus,
    }
