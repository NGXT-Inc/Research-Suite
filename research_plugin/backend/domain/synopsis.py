"""Validation for the reviewer-submitted synopsis (researcher TLDR)."""

from __future__ import annotations

import re

SYNOPSIS_MIN_LEN = 40
SYNOPSIS_MAX_LEN = 420

# Catches entity ids leaking into reader-facing prose, e.g. "exp_3f2a".
_ENTITY_ID_RE = re.compile(r"\b(exp|claim|res|rev|rver|syn)_[A-Za-z0-9]")


def validate_synopsis(value: str) -> str:
    """Return the stripped synopsis or raise ``ValueError`` with agent-facing guidance."""
    synopsis = value.strip()
    length_hint = (
        "synopsis is the researcher's TLDR: 1-3 plain sentences, 40-420 "
        "chars, no entity ids or markdown — describe what happened in "
        "human terms"
    )
    if not (SYNOPSIS_MIN_LEN <= len(synopsis) <= SYNOPSIS_MAX_LEN):
        raise ValueError(length_hint)
    if "\n" in synopsis:
        raise ValueError(f"{length_hint} (no newlines — keep it to one line)")
    if "`" in synopsis:
        raise ValueError(f"{length_hint} (no backticks — plain prose only)")
    if synopsis.startswith("#"):
        raise ValueError(f"{length_hint} (no markdown headings)")
    if _ENTITY_ID_RE.search(synopsis):
        raise ValueError(
            f"{length_hint} (no entity ids like exp_/claim_/res_/rev_/rver_/syn_ "
            "— name things by their human names instead)"
        )
    return synopsis
