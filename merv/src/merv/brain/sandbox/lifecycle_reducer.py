"""Pure sandbox lifecycle decisions.

Provider calls and persistence belong to the lifecycle executor.  This module
only turns observed facts into an ordered set of effects plus the durable event
that describes the decision.  Keeping that split explicit makes the dangerous
rule easy to test: an unknown provider outcome never becomes a terminal row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


IntentKind = Literal[
    "cleanup_orphan",
    "mark_failed",
    "mark_terminated",
    "refresh_endpoint",
    "touch_alive",
]


@dataclass(frozen=True, slots=True)
class SideEffectIntent:
    kind: IntentKind
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    intents: tuple[SideEffectIntent, ...] = ()
    event: LifecycleEvent | None = None


def reconcile_decision(
    *, row: Mapping[str, Any], alive: bool | None, job_live: bool = False
) -> LifecycleDecision:
    """Decide how one observed row should converge toward provider reality."""
    status = str(row.get("status") or "")
    sandbox_id = str(row.get("sandbox_id") or "")
    sandbox_uid = str(row.get("sandbox_uid") or "")
    if status == "running" and sandbox_id:
        if alive is None:
            return LifecycleDecision()
        if alive:
            return LifecycleDecision(
                intents=(
                    SideEffectIntent("touch_alive", {}),
                    SideEffectIntent("refresh_endpoint", {}),
                )
            )
        return LifecycleDecision(
            intents=(SideEffectIntent("mark_terminated", {}),),
            event=LifecycleEvent(
                "sandbox.expired",
                {"sandbox_id": sandbox_id, "sandbox_uid": sandbox_uid},
            ),
        )
    if status == "provisioning" and not job_live:
        error = "provisioning interrupted; call sandbox.request again"
        return LifecycleDecision(
            intents=(
                SideEffectIntent("cleanup_orphan", {}),
                SideEffectIntent("mark_failed", {"error": error}),
            ),
            event=LifecycleEvent(
                "sandbox.failed", {"error": "provisioning interrupted"}
            ),
        )
    return LifecycleDecision()


def reap_decision(
    *,
    row: Mapping[str, Any],
    outcome: Literal["stopped", "gone", "maybe_alive"],
    event_type: str,
    payload_extra: Mapping[str, Any] | None = None,
) -> LifecycleDecision:
    """Describe a reap after the provider termination attempt has completed."""
    sandbox_id = str(row.get("sandbox_id") or "")
    sandbox_uid = str(row.get("sandbox_uid") or "")
    extra = dict(payload_extra or {})
    if outcome == "maybe_alive":
        return LifecycleDecision(
            event=LifecycleEvent(
                event_type,
                {
                    "sandbox_id": sandbox_id,
                    "sandbox_uid": sandbox_uid,
                    "reaped": False,
                    "reason": "terminate failed; instance may still be alive",
                    **extra,
                },
            )
        )
    return LifecycleDecision(
        intents=(SideEffectIntent("mark_terminated", {}),),
        event=LifecycleEvent(
            event_type,
            {
                "sandbox_id": sandbox_id,
                "sandbox_uid": sandbox_uid,
                "reaped": True,
                "expires_at": row.get("expires_at"),
                "stopped": outcome == "stopped",
                **extra,
            },
        ),
    )


def release_decision(
    *,
    row: Mapping[str, Any],
    outcome: Literal["stopped", "gone", "maybe_alive"],
    active_experiment_ids: list[str],
) -> LifecycleDecision:
    """Describe an explicitly confirmed release."""
    sandbox_id = str(row.get("sandbox_id") or "")
    sandbox_uid = str(row.get("sandbox_uid") or "")
    if outcome == "maybe_alive":
        return LifecycleDecision(
            event=LifecycleEvent(
                "sandbox.release_failed",
                {
                    "sandbox_id": sandbox_id,
                    "sandbox_uid": sandbox_uid,
                    "reason": "terminate failed; instance may still be alive",
                },
            )
        )
    return LifecycleDecision(
        intents=(SideEffectIntent("mark_terminated", {}),),
        event=LifecycleEvent(
            "sandbox.released",
            {
                "sandbox_id": sandbox_id,
                "sandbox_uid": sandbox_uid,
                "active_experiment_ids": list(active_experiment_ids),
                "stopped": outcome == "stopped",
            },
        ),
    )


__all__ = [
    "LifecycleDecision",
    "LifecycleEvent",
    "SideEffectIntent",
    "reap_decision",
    "reconcile_decision",
    "release_decision",
]
