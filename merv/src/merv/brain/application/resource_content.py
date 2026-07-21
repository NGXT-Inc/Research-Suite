"""Hosted resource-content response shaping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..artifacts.facade import Artifacts
from ..kernel.utils import WorkflowError

Record = dict[str, Any]


@dataclass(slots=True)
class HostedResourceContentQuery:
    """Present authorized submitted text without reading a local checkout."""

    artifacts: Artifacts

    def __call__(
        self, *, project_id: str, resource_id: str, version_id: str | None = None
    ) -> Record:
        resource = self.artifacts.resolve_resource(
            project_id=project_id, resource_id=resource_id
        )
        explicit_version = bool(version_id)
        try:
            selected = self.artifacts.select_resource_text(
                resource=resource,
                version_id=version_id if explicit_version else None,
            )
        except WorkflowError as exc:
            if not explicit_version:
                raise
            return self._unavailable(
                resource=resource,
                reason="version_unavailable",
                detail=str(exc),
                version_id=str(version_id),
            )
        if selected is None:
            return self._unavailable(resource=resource)
        text, selected_version_id = selected
        response = {
            "resource": resource,
            "path": resource.get("path"),
            "content": text,
            "text": text,
            "size_bytes": len(text.encode("utf-8")),
            "source": "submitted",
            "version_id": selected_version_id,
        }
        if explicit_version:
            response["available"] = True
        return response

    @staticmethod
    def _unavailable(
        *,
        resource: Record,
        reason: str = "content_unavailable_in_this_mode",
        detail: str | None = None,
        version_id: str | None = None,
    ) -> Record:
        response = {
            "resource": resource,
            "path": resource.get("path"),
            "content": None,
            "text": None,
            "available": False,
            "source": "unavailable",
            "reason": reason,
            "detail": detail or (
                "this file's bytes live only on the local data plane; "
                "result-role files are metadata-only in this mode"
            ),
        }
        if version_id is not None:
            response["version_id"] = version_id
        return response


__all__ = ["HostedResourceContentQuery"]
