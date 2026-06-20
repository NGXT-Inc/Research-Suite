"""Submit-style sandbox task channel port."""

from __future__ import annotations

from typing import Any, Protocol


class TaskChannel(Protocol):
    def submit(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        deadline: str | None = None,
        tenant_id: str | None = None,
    ) -> Any:
        ...
