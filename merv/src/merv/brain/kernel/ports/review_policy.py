"""Permission policy used by the review service."""

from __future__ import annotations

from typing import Protocol


class ReviewPolicy(Protocol):
    """Validates review vocabulary accepted by review requests/submissions."""

    def validate_review_role(self, *, role: str) -> None:
        ...

    def validate_review_verdict(self, *, verdict: str) -> None:
        ...
