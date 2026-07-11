"""Advisory anomaly detection over compat-view MLflow run records.

Advisories are observations, not instructions. When a metric history looks
wrong — non-finite values, a directional metric moving away from its best, a
long stretch without improvement while the run is live — the system states
what it saw and why that pattern is usually worth a look. It never prescribes
an action: whether anything is actually wrong, and what to do about it, is
the agent's call.

Detection is deterministic over the bounded snapshot shape produced by
``metrics.snapshot_mlflow`` (``history`` is ``{key: [[step, value|None],
...]}`` where non-finite raw values arrive as ``None``), so the same run
state always yields the same advisories and tests need no MLflow server.
"""

from __future__ import annotations

import re
from typing import Any

ADVISORY_NOTE = (
    "Advisories are observations, not instructions: the system takes no "
    "action and prescribes none. Investigate the flagged runs and decide "
    "what, if anything, is wrong."
)

# Detection thresholds. Conservative on purpose: a false alarm costs agent
# attention and erodes trust in the channel, while a borderline miss still
# surfaces one read later once the pattern grows unambiguous.
MIN_TREND_POINTS = 8  # fewer finite points can't separate trend from noise
RECENT_WINDOW_FRACTION = 0.2  # "recent" = trailing 20% of points (min 3)
DIVERGENCE_RANGE_FRACTION = 0.35  # recent this far off best, in range units
PLATEAU_MIN_POINTS = 12  # plateaus need a longer record than divergence
PLATEAU_RANGE_FRACTION = 0.03  # tail improvement below this fraction = flat

_DOWN_GOOD = re.compile(r"loss|err|perplexity|bpb|bpc", re.IGNORECASE)
_UP_GOOD = re.compile(r"acc|score|reward|f1|auc|mfu", re.IGNORECASE)
# Exit/return codes are pass-fail diagnostics; trend detectors on them would
# read a 0→1 flip as "divergence" instead of the failure it plainly is.
_DIAGNOSTIC_KEY = re.compile(r"_exit(?:_code)?$|_code$")


def good_direction(key: str, params: dict[str, Any] | None = None) -> int:
    """Which way is good for a metric: -1 down, +1 up, 0 unknown.

    A run-declared contract (``primary_metric`` + ``primary_metric_direction``
    params) beats the name convention — same precedence as the UI ledger.
    """
    params = params or {}
    if str(params.get("primary_metric") or "") == key:
        declared = str(params.get("primary_metric_direction") or "").lower()
        if re.search(r"min|down|lower", declared):
            return -1
        if re.search(r"max|up|higher", declared):
            return 1
    if _DOWN_GOOD.search(key):
        return -1
    if _UP_GOOD.search(key):
        return 1
    return 0


def advisory_fingerprint(advisory: dict[str, Any]) -> str:
    """Stable identity for dedup: the same problem on the same run's metric
    is one advisory, however many reads observe it."""
    return (
        f"{advisory.get('run_id') or ''}:"
        f"{advisory.get('metric') or ''}:"
        f"{advisory.get('code') or ''}"
    )


def detect_snapshot_advisories(
    snapshot: dict[str, Any] | None, *, window_started_ms: int | None = None
) -> list[dict[str, Any]]:
    """All advisories across a snapshot's runs, warnings first.

    ``window_started_ms`` scopes detection to the current attempt (same
    filter as the metrics exhibit) so a prior attempt's runs don't nag the
    one now executing.
    """
    if not isinstance(snapshot, dict):
        return []
    found: list[dict[str, Any]] = []
    for experiment in snapshot.get("experiments") or []:
        for run in experiment.get("runs") or []:
            if not isinstance(run, dict):
                continue
            start = run.get("start_time")
            if (
                window_started_ms is not None
                and isinstance(start, (int, float))
                and start < window_started_ms
            ):
                continue
            found.extend(detect_run_advisories(run))
    found.sort(key=lambda a: (a.get("severity") != "warning", a.get("metric") or ""))
    return found


def detect_run_advisories(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Advisories for one compat-view run record."""
    history = run.get("history") or {}
    params = run.get("params") or {}
    running = str(run.get("status") or "").upper() == "RUNNING"
    found: list[dict[str, Any]] = []
    for key in sorted(history):
        points = [
            point
            for point in (history.get(key) or [])
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        if not points:
            continue
        non_finite = [point for point in points if not isinstance(point[1], (int, float))]
        finite = [point for point in points if isinstance(point[1], (int, float))]
        if non_finite:
            found.append(_advisory(
                run,
                key,
                code="non_finite_values",
                severity="warning",
                summary=(
                    f"{key} logged non-finite values "
                    f"({len(non_finite)} of {len(points)} points)"
                ),
                reasoning=(
                    "NaN/Inf in a metric usually points at numerical "
                    "instability — overflow, a too-hot learning rate, log(0), "
                    f"or corrupted inputs. First non-finite {key} at step "
                    f"{non_finite[0][0]}."
                ),
                evidence={
                    "non_finite_points": len(non_finite),
                    "total_points": len(points),
                    "first_non_finite_step": non_finite[0][0],
                },
            ))
        direction = good_direction(key, params)
        if direction == 0 or _DIAGNOSTIC_KEY.search(key) or len(finite) < MIN_TREND_POINTS:
            continue
        found.extend(_trend_advisories(
            run, key, finite=finite, direction=direction, running=running
        ))
    return found


def _trend_advisories(
    run: dict[str, Any],
    key: str,
    *,
    finite: list[Any],
    direction: int,
    running: bool,
) -> list[dict[str, Any]]:
    # Oriented so smaller is always better; values convert back for display.
    oriented = [value if direction < 0 else -value for _, value in finite]
    span = max(oriented) - min(oriented)
    if span <= 0:
        return []
    display = (lambda w: w) if direction < 0 else (lambda w: -w)
    word = "lower" if direction < 0 else "higher"
    best = min(oriented)
    best_index = oriented.index(best)
    best_step = finite[best_index][0]
    recent_count = max(3, round(len(oriented) * RECENT_WINDOW_FRACTION))
    recent = _median(oriented[-recent_count:])
    drift = recent - best

    # Divergence: the best is not recent and the recent median sits well
    # above it relative to everything this metric has ever spanned.
    if best_index < len(oriented) - recent_count and drift > DIVERGENCE_RANGE_FRACTION * span:
        return [_advisory(
            run,
            key,
            code="metric_diverging",
            severity="warning",
            summary=(
                f"{key} is moving away from its best "
                f"({_fmt(display(best))} at step {best_step} → "
                f"{_fmt(display(recent))} recently)"
            ),
            reasoning=(
                f"The recent median is {round(100 * drift / span)}% of the "
                f"metric's observed range worse than its best, and the best "
                f"is not recent. For a {word}-is-better metric that shape "
                "often means divergence or a training regression rather than "
                "noise."
            ),
            evidence={
                "best": display(best),
                "best_step": best_step,
                "recent_median": display(recent),
                "recent_points": recent_count,
                "drift_fraction_of_range": round(drift / span, 4),
            },
        )]

    # Plateau: only meaningful while the run is live — a finished run
    # resting at its converged value is the normal shape of success.
    if running and len(oriented) >= PLATEAU_MIN_POINTS:
        best_first_half = min(oriented[: len(oriented) // 2])
        improvement = best_first_half - best
        if improvement <= PLATEAU_RANGE_FRACTION * span:
            since_step = finite[len(finite) // 2][0]
            return [_advisory(
                run,
                key,
                code="metric_plateau",
                severity="notice",
                summary=(
                    f"{key} has not meaningfully improved since step "
                    f"{since_step} (best {_fmt(display(best))} was already "
                    "reached in the first half of logged steps)"
                ),
                reasoning=(
                    "A long flat stretch on a live run can mean converged, "
                    "stuck, or starved — the trajectory alone can't tell "
                    "which."
                ),
                evidence={
                    "best": display(best),
                    "best_step": best_step,
                    "first_half_best": display(best_first_half),
                    "improvement_fraction_of_range": round(improvement / span, 4),
                    "points": len(oriented),
                },
            )]
    return []


def _advisory(
    run: dict[str, Any],
    metric: str,
    *,
    code: str,
    severity: str,
    summary: str,
    reasoning: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "metric": metric,
        "run_id": str(run.get("run_id") or ""),
        "run_name": str(run.get("run_name") or ""),
        "run_status": str(run.get("status") or ""),
        "summary": summary,
        "reasoning": reasoning,
        "evidence": evidence,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _fmt(value: float) -> str:
    return f"{value:.6g}"
