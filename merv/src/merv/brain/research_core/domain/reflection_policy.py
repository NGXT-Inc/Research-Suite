"""Shared project-reflection thresholds."""

from collections.abc import Mapping
from typing import Any

# The project gets an advisory nudge before it gets a hard workflow block. Keep
# the two numbers separate so urgent follow-up experiments remain possible until
# the hard cap is hit.
REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD = 3
REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD = 5


def covered_terminal_ids(corpus: Mapping[str, object] | None) -> set[str]:
    """Ids of terminal experiments a published reflection corpus already covers.

    Single source of truth for reflection-drift: callers diff this against the
    project's current terminal experiments. Tolerates a missing/empty corpus
    and non-dict list entries so a malformed snapshot never raises here."""
    if not corpus:
        return set()
    entries = corpus.get("terminal_experiments") or []
    return {
        str(exp.get("id"))
        for exp in entries
        if isinstance(exp, Mapping)
    }


def reflection_signal_state(
    *,
    current_terminal: Mapping[str, str],
    current_claims: Mapping[str, str],
    published: Mapping[str, Any] | None,
    open_wave: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Pure reflection-drift signal from queried project state."""
    covered_ids = covered_terminal_ids(
        None if published is None else (published.get("corpus") or {})
    )
    if published is None:
        snapshot_claims: dict[str, str] = {}
    else:
        corpus = published.get("corpus") or {}
        snapshot_claims = {
            str(claim.get("id")): str(claim.get("status"))
            for claim in corpus.get("claims", [])
            if isinstance(claim, Mapping)
        }

    new_terminal = sorted(set(current_terminal) - covered_ids)
    claims_changed = [
        {"id": cid, "from": snapshot_claims.get(cid), "to": status}
        for cid, status in sorted(current_claims.items())
        if published is not None and snapshot_claims.get(cid) != status
    ]
    contradicted_flip = any(
        change["to"] == "contradicted" for change in claims_changed
    )
    experiment_create_blocked = (
        len(new_terminal) >= REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD
    )
    stale = open_wave is None and (
        len(new_terminal) >= REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD
        or contradicted_flip
    )
    return {
        "terminal_experiments": len(current_terminal),
        "covered_terminal_experiments": len(covered_ids & set(current_terminal)),
        "new_terminal_since_publish": len(new_terminal),
        "claims_changed_since_publish": len(claims_changed),
        "contradicted_flip": contradicted_flip,
        "last_published_at": (published or {}).get("published_at"),
        "last_published_reflection_id": (published or {}).get("id"),
        "open_reflection_id": (open_wave or {}).get("id"),
        "stale": stale,
        "experiment_create_blocked": experiment_create_blocked,
        "nudge_new_terminal_threshold": REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
        "block_new_terminal_threshold": REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    }


def reflection_create_block_message(
    *,
    debt: int,
    published_id: str | None,
    open_wave: Mapping[str, Any] | None,
    threshold: int = REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
) -> str | None:
    if debt < threshold:
        return None
    if open_wave is not None:
        return (
            "project reflection is required before creating another experiment: "
            f"{debt} experiments have finished since the last published "
            f"reflection (threshold {threshold}), and reflection wave "
            f"{open_wave['id']} is {open_wave['status']!r}. Finish and publish "
            "that reflection wave; its approved change spec will create the "
            "next experiment wave."
        )
    if published_id:
        since = "since the last published reflection"
    else:
        since = "and no project reflection has been published yet"
    return (
        "project reflection is required before creating another experiment: "
        f"{debt} experiments have finished {since} (threshold {threshold}). "
        "Start a reflection wave with reflection.create and publish it before "
        "creating another experiment."
    )
