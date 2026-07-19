"""Pure helpers for choosing associated resources."""

from __future__ import annotations

from typing import Any


def preferred_associated_resource(
    *,
    resources: list[dict[str, Any]],
    attempt: Any,
    roles: tuple[str, ...],
) -> dict[str, Any] | None:
    """Resource association to render: current attempt, canonical role, newest."""
    candidates = [
        resource
        for resource in resources
        if resource.get("association_role") in roles
    ]
    if not candidates:
        return None
    current = [
        resource
        for resource in candidates
        if resource.get("association_attempt_index") == attempt
    ]
    pool = current or candidates
    role_rank = {role: index for index, role in enumerate(roles)}
    return min(
        pool,
        key=lambda resource: (
            role_rank.get(str(resource.get("association_role")), len(roles)),
            -(resource.get("association_rowid") or 0),
        ),
    )
