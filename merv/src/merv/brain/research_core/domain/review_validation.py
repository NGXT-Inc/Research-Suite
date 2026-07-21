"""Review vocabulary validation owned by Research."""

from __future__ import annotations

from ...kernel.utils import ValidationError
from .vocabulary import REVIEW_ROLES, REVIEW_VERDICTS


def validate_review_role(*, role: str) -> None:
    if role not in REVIEW_ROLES:
        raise ValidationError(f"unknown review role: {role}")


def validate_review_verdict(*, verdict: str) -> None:
    if verdict not in REVIEW_VERDICTS:
        raise ValidationError(f"unknown review verdict: {verdict}")
