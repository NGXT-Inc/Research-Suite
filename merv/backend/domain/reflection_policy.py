"""Shared project-reflection thresholds."""

from collections.abc import Iterable, Mapping
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


def terminal_drift_count(
    *, current_terminal_ids: Iterable[str], corpus: Mapping[str, object] | None
) -> int:
    """Count of current terminal experiments not yet covered by the corpus."""
    return len(set(current_terminal_ids) - covered_terminal_ids(corpus))


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
    signal: dict[str, Any] = {
        "terminal_experiments": len(current_terminal),
        "covered_terminal_experiments": len(covered_ids & set(current_terminal)),
        "new_terminal_since_publish": len(new_terminal),
        "claims_changed_since_publish": len(claims_changed),
        "contradicted_flip": contradicted_flip,
        "last_published_at": (published or {}).get("published_at"),
        "last_published_synthesis_id": (published or {}).get("id"),
        "open_reflection_id": (open_wave or {}).get("id"),
        "stale": stale,
        "experiment_create_blocked": experiment_create_blocked,
        "nudge_new_terminal_threshold": REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
        "block_new_terminal_threshold": REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    }
    signal["hint"] = reflection_staleness_hint(
        signal=signal, published_exists=published is not None
    )
    return signal


def reflection_staleness_hint(
    *, signal: Mapping[str, Any], published_exists: bool
) -> str:
    if not signal["stale"]:
        return ""
    if signal.get("experiment_create_blocked"):
        if not published_exists:
            return (
                "Project reflection required before creating another "
                "experiment — "
                f"{signal['terminal_experiments']} experiments have finished "
                "and no project reflection exists yet. Use the "
                "project-reflection skill (reflection.create) and publish the "
                "wave before creating another experiment."
            )
        pieces = [
            "Project reflection required before creating another experiment — "
            f"{signal['new_terminal_since_publish']} experiments have finished "
            "since the last published reflection"
        ]
        if signal["claims_changed_since_publish"]:
            changed = f"{signal['claims_changed_since_publish']} claims have changed"
            if signal["contradicted_flip"]:
                changed += " (including a claim now contradicted)"
            pieces.append(changed)
        pieces.append(
            "the current reflection covers "
            f"{signal['covered_terminal_experiments']} of "
            f"{signal['terminal_experiments']} finished experiments"
        )
        return (
            "; ".join(pieces)
            + ". Publish a project reflection wave before creating another "
            "experiment."
        )
    if not published_exists:
        return (
            "Consider running the project's first reflection — "
            f"{signal['terminal_experiments']} experiments have finished and "
            "no project reflection exists yet. Use the project-reflection "
            "skill (reflection.create) when you judge the time is right."
        )
    pieces = [
        "Consider running a project reflection — "
        f"{signal['new_terminal_since_publish']} experiments have finished "
        "since the last published reflection"
    ]
    if signal["claims_changed_since_publish"]:
        changed = f"{signal['claims_changed_since_publish']} claims have changed"
        if signal["contradicted_flip"]:
            changed += " (including a claim now contradicted)"
        pieces.append(changed)
    pieces.append(
        "the current reflection covers "
        f"{signal['covered_terminal_experiments']} of "
        f"{signal['terminal_experiments']} finished experiments"
    )
    return (
        "; ".join(pieces)
        + ". Whether these developments change the project's logic state is "
        "your call (project-reflection skill, reflection.create)."
    )


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


def reflection_create_block_reason(*, signal: Mapping[str, Any]) -> str:
    count = signal.get("new_terminal_since_publish", 0)
    threshold = signal.get("block_new_terminal_threshold", 5)
    open_id = signal.get("open_reflection_id") or signal.get("open_reflection_id")
    if open_id:
        return (
            f"{count} experiments have finished since the last published "
            f"reflection (threshold {threshold}); finish and publish open "
            f"reflection wave {open_id} before creating another experiment."
        )
    if signal.get("last_published_synthesis_id") or signal.get(
        "last_published_reflection_id"
    ):
        since = "since the last published reflection"
    else:
        since = "and no project reflection has been published yet"
    return (
        f"{count} experiments have finished {since} (threshold {threshold}); "
        "publish a project reflection wave before creating another experiment."
    )


def external_reflection_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(signal)
    if "last_published_synthesis_id" in output:
        output["last_published_reflection_id"] = output.pop(
            "last_published_synthesis_id"
        )
    if "open_reflection_id" in output:
        output["open_reflection_id"] = output.pop("open_reflection_id")
    return output


def idle_reflection_hint(*, signal: Mapping[str, Any]) -> str:
    """Hint for the idle recommended tier below the staleness threshold."""
    new = signal["new_terminal_since_publish"]
    finished = f"{new} experiment{'s have' if new != 1 else ' has'} finished"
    if signal["last_published_synthesis_id"]:
        drift = f"{finished} since the last published reflection"
        if signal["claims_changed_since_publish"]:
            drift += (
                f" and {signal['claims_changed_since_publish']} claims "
                "have changed"
            )
    else:
        drift = f"{finished} and no project reflection exists yet"
    return (
        f"No experiments are active and {drift} — a good moment for a "
        "project reflection (reflection.create, project-reflection "
        "skill), or start the next experiment if the logic state is "
        "current."
    )


def post_publish_guidance(
    *, materialized_experiments: list[Mapping[str, Any]]
) -> dict[str, Any]:
    experiments = [
        {
            "experiment_id": row.get("experiment_id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "folder": f"experiments/{row.get('name')}/",
            "intent": row.get("intent"),
        }
        for row in materialized_experiments
    ]
    count = len(experiments)
    noun = "experiment" if count == 1 else "experiments"
    return {
        "summary": (
            f"Reflection publish created {count} planned {noun}. "
            "Materialize their local folders before editing files, then "
            "call workflow.status_and_next for the experiment you start."
        ),
        "experiments": experiments,
        "recommended_actions": [
            {
                "tool": "experiment.materialize_folders",
                "arguments": {"status": "planned"},
                "why": "Create local folders for the newly planned experiment wave.",
            },
            {
                "tool": "workflow.status_and_next",
                "arguments": {"experiment_id": experiments[0]["experiment_id"]},
                "why": "Start with the first newly planned experiment.",
            },
        ],
    }
