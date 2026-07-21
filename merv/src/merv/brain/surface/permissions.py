"""Authorization at the external tool boundary."""

from __future__ import annotations

from ..kernel.utils import PermissionDeniedError


class PermissionService:
    """Reject mutations attempted through a read-only reviewer capability."""

    def reject_reviewer_mutation(
        self, *, tool_name: str, review_session_id: str | None
    ) -> None:
        if review_session_id and tool_name != "review.submit":
            raise PermissionDeniedError(
                "review sessions are read-only except review.submit"
            )
