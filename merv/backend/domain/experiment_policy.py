"""Shared experiment workflow limits."""

from __future__ import annotations

import re

from ..utils import ValidationError

ACTIVE_EXPERIMENT_CAP = 7

# Claim-status inference markers: (pattern, plain vote, vote when negated in
# the same clause). A None vote means the direction is unclear.
_CLAIM_STATUS_MARKERS: tuple[tuple[re.Pattern[str], str | None, str | None], ...] = (
    (re.compile(r"\bcontradict\w*"), "contradicted", None),
    (re.compile(r"\brefut\w*"), "contradicted", None),
    (re.compile(r"\bfalsif\w*"), "contradicted", None),
    (re.compile(r"\bdisprov\w*"), "contradicted", None),
    (re.compile(r"\bsupport(?:s|ed|ing)?\b"), "supported", "weakened"),
    (re.compile(r"\bconfirm\w*"), "supported", "weakened"),
    (re.compile(r"\bbeats?\b"), "supported", "weakened"),
    (re.compile(r"\bimprov\w*"), "supported", "weakened"),
    (re.compile(r"\bpositive result\w*"), "supported", "weakened"),
    (re.compile(r"\b(?:target|criterion|criteria|threshold) met\b"), "supported", "weakened"),
    (re.compile(r"\bunsupported\b"), "weakened", None),
    (re.compile(r"\bnegative result\w*"), "weakened", None),
    (re.compile(r"\bweaken\w*"), "weakened", None),
    (re.compile(r"\binconclusive\b"), "weakened", None),
    (re.compile(r"\bmixed (?:results?|evidence|findings|signals?)\b"), "weakened", None),
    (re.compile(r"\bpartial(?:ly)? support\w*"), "weakened", None),
    (re.compile(r"\bno (?:significant )?effect\b"), "weakened", None),
    (re.compile(r"\bnot significant\b|\binsignificant\b"), "weakened", None),
    (re.compile(r"\bbelow (?:the )?baseline\b"), "weakened", None),
    (re.compile(r"\bbeaten\b"), "weakened", None),
    (re.compile(r"\bworse than\b"), "weakened", None),
    (re.compile(r"\bunderperform\w*"), "weakened", None),
)

_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|neither|nor|without|cannot|can't|couldn't|didn't|"
    r"doesn't|wasn't|weren't|fail(?:ed|s)?(?:\s+to)?|unable\s+to|far\s+from)\b"
)
_CLAUSE_BOUNDARIES = (". ", "; ", ", ", " but ", " however ", " although ", " yet ")


def active_experiment_cap_reached_message(*, active_count: int) -> str:
    return (
        "active experiment cap reached: "
        f"project has {active_count} active experiments; "
        "finish one before creating another."
    )


def active_experiment_cap_would_exceed_message(
    *, active_count: int, proposed_count: int
) -> str:
    experiment_word = "experiment" if proposed_count == 1 else "experiments"
    return (
        "active experiment cap would be exceeded: "
        f"project has {active_count} active experiments and this reflection "
        f"proposes {proposed_count} new {experiment_word}; "
        "finish one before creating another."
    )


def compose_experiment_intent(
    *,
    intent: str,
    title: str,
    hypothesis: str,
    design: str,
    success_criteria: str,
    risks: str,
) -> str:
    """Durable experiment headline with back-compat fallbacks."""
    if intent.strip():
        return intent.strip()
    for value in (title, hypothesis, design, success_criteria, risks):
        if value and value.strip():
            return value.strip()
    return ""


def normalize_claim_ids(
    *,
    tested_claim_ids: list[str] | str | None,
    claim_id: str | None,
    claim_ids: list[str] | str | None,
) -> list[str]:
    values: list[str] = []
    if isinstance(tested_claim_ids, str):
        values.append(tested_claim_ids)
    elif tested_claim_ids:
        values.extend(tested_claim_ids)
    if claim_id:
        values.append(claim_id)
    if isinstance(claim_ids, str):
        values.append(claim_ids)
    elif claim_ids:
        values.extend(claim_ids)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("claim ids must be non-empty strings")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def infer_claim_status_from_conclusion(conclusion: str) -> str | None:
    """Conservative status hint from a free-text conclusion."""
    text = " ".join(conclusion.lower().split())
    votes: set[str] = set()
    for pattern, plain_vote, negated_vote in _CLAIM_STATUS_MARKERS:
        for match in pattern.finditer(text):
            vote = negated_vote if _negated_in_clause(text, match.start()) else plain_vote
            if vote is None:
                return None
            votes.add(vote)
    if len(votes) != 1:
        return None
    return votes.pop()


def _negated_in_clause(text: str, match_start: int) -> bool:
    window = text[max(0, match_start - 40):match_start]
    for boundary in _CLAUSE_BOUNDARIES:
        idx = window.rfind(boundary)
        if idx >= 0:
            window = window[idx + len(boundary):]
    return bool(_NEGATION_RE.search(window))
