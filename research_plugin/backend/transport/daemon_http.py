"""Control-side HTTP endpoints used by local data-plane daemons."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any

from fastapi import Body, Query, Request

from ..services.feed import MAX_IMAGE_BYTES
from ..utils import ValidationError

JsonBody = dict[str, Any] | None
DaemonPrincipalRequired = Callable[[Request], Any]
DaemonProjectApp = Callable[[Request, str], Any]


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value) == "":
        raise ValidationError(f"{key} is required")
    return str(value)


def _decode_b64_field(
    value: Any, *, label: str, max_decoded_bytes: int | None = None
) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be non-empty base64")
    if max_decoded_bytes is not None:
        max_encoded_chars = ((max_decoded_bytes + 2) // 3) * 4
        if len(value) > max_encoded_chars:
            raise ValidationError(
                f"{label} decodes above the {max_decoded_bytes} byte limit"
            )
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValidationError(f"{label} must be valid base64") from exc
    if max_decoded_bytes is not None and len(data) > max_decoded_bytes:
        raise ValidationError(
            f"{label} decodes to {len(data)} bytes; limit is {max_decoded_bytes}"
        )
    return data


def register_daemon_routes(
    http: Any,
    *,
    task_queue: Any | None,
    sync_targets_source: Any | None,
    require_daemon: DaemonPrincipalRequired,
    app_for_project: DaemonProjectApp,
) -> None:
    if task_queue is not None:

        @http.get("/api/daemon/tasks")
        def daemon_poll_tasks(
            request: Request,
            client_id: str = Query(""),  # noqa: ARG001 - v1 single daemon per tenant
            wait: int = Query(25, ge=0, le=60),
        ) -> dict[str, Any]:
            principal = require_daemon(request)
            task = task_queue.poll(
                wait_seconds=float(wait),
                tenant_id=getattr(principal, "tenant_id", "") or "",
            )
            return {"task": task}

        @http.post("/api/daemon/tasks/{task_id}/ack")
        def daemon_ack_task(
            task_id: str, request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            principal = require_daemon(request)
            payload = body or {}
            task_queue.ack(
                task_id=task_id,
                ok=bool(payload.get("ok")),
                result=payload.get("result"),
                error=payload.get("error"),
                tenant_id=getattr(principal, "tenant_id", "") or "",
            )
            return {"acked": True}

        @http.post("/api/daemon/resources/validate-association")
        def daemon_validate_resource_association(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            return app.resources.validate_association_intent(
                project_id=project_id,
                resource_id=_required_text(payload, "resource_id"),
                target_type=_required_text(payload, "target_type"),
                target_id=_required_text(payload, "target_id"),
                role=_required_text(payload, "role"),
            )

        @http.post("/api/daemon/resources/observe")
        def daemon_observe_resource(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            return app.resources.record_observation(
                project_id=project_id,
                path=_required_text(payload, "path"),
                kind=str(payload.get("kind") or "other"),
                title=str(payload.get("title") or ""),
                created_by=str(payload.get("created_by") or "codex"),
                mtime_ns=int(payload.get("mtime_ns") or 0),
                ctime_ns=int(payload.get("ctime_ns") or 0),
                size_bytes=int(payload.get("size_bytes") or 0),
                content_sha256=_required_text(payload, "content_sha256"),
                content_type=str(
                    payload.get("content_type") or "application/octet-stream"
                ),
            )

        @http.post("/api/daemon/resources/associate")
        def daemon_associate_resource(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            app.resources.validate_association_intent(
                project_id=project_id,
                resource_id=_required_text(payload, "resource_id"),
                target_type=_required_text(payload, "target_type"),
                target_id=_required_text(payload, "target_id"),
                role=_required_text(payload, "role"),
            )
            blob = payload.get("blob")
            content_bytes = None
            if isinstance(blob, dict):
                content_bytes = _decode_b64_field(
                    blob.get("data_b64"), label="blob.data_b64"
                )
            figures: list[dict[str, Any]] = []
            for index, figure in enumerate(payload.get("figures") or []):
                if not isinstance(figure, dict):
                    raise ValidationError(f"figures[{index}] must be an object")
                figures.append(
                    {
                        "link_path": _required_text(figure, "link_path"),
                        "data": _decode_b64_field(
                            figure.get("data_b64"),
                            label=f"figures[{index}].data_b64",
                        ),
                        "content_type": str(
                            figure.get("content_type") or "application/octet-stream"
                        ),
                    }
                )
            return app.resources.associate_observed(
                project_id=project_id,
                resource_id=_required_text(payload, "resource_id"),
                target_type=_required_text(payload, "target_type"),
                target_id=_required_text(payload, "target_id"),
                role=_required_text(payload, "role"),
                content_bytes=content_bytes,
                figures=figures,
            )

        @http.post("/api/daemon/feed/validate-post")
        def daemon_validate_feed_post(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            return app.feed.validate_post_intent(
                project_id=project_id,
                handle=_required_text(payload, "handle"),
                text=_required_text(payload, "text"),
                ref=payload.get("ref"),
            )

        @http.post("/api/daemon/sandboxes/request")
        def daemon_request_sandbox(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            experiment_id = _required_text(payload, "experiment_id")
            app = app_for_project(request, project_id)
            result = app.sandboxes.request_from_data_plane(
                project_id=project_id,
                experiment_id=experiment_id,
                public_key=_required_text(payload, "public_key"),
                gpu=payload.get("gpu"),
                cpu=payload.get("cpu"),
                memory=payload.get("memory"),
                time_limit=payload.get("time_limit"),
                instance_type=payload.get("instance_type"),
                region=payload.get("region"),
            )
            result["_experiment_name"] = app.sandboxes.registry.experiment_name(
                experiment_id=experiment_id
            )
            return result

        @http.post("/api/daemon/sandboxes/sync")
        def daemon_sync_sandbox(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            return app.sandboxes.sync(
                project_id=project_id,
                experiment_id=_required_text(payload, "experiment_id"),
                daemon_metrics_snapshot=payload.get("metrics_snapshot"),
                daemon_metrics_snapshot_provided="metrics_snapshot" in payload,
            )

        @http.post("/api/daemon/sandboxes/metrics")
        def daemon_sandbox_metrics(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            snapshot = payload.get("metrics_snapshot")
            return app.sandboxes.record_daemon_metrics(
                project_id=project_id,
                experiment_id=_required_text(payload, "experiment_id"),
                snapshot=snapshot if isinstance(snapshot, dict) else None,
            )

        @http.post("/api/daemon/feed/post")
        def daemon_post_feed(
            request: Request, body: JsonBody = Body(default=None)
        ) -> dict[str, Any]:
            payload = body or {}
            project_id = _required_text(payload, "project_id")
            app = app_for_project(request, project_id)
            app.feed.validate_post_intent(
                project_id=project_id,
                handle=_required_text(payload, "handle"),
                text=_required_text(payload, "text"),
                ref=payload.get("ref"),
            )
            image = payload.get("image")
            image_bytes = None
            image_path = None
            if image is not None:
                if not isinstance(image, dict):
                    raise ValidationError("image must be an object")
                image_path = str(image.get("path") or "feed-image")
                image_bytes = _decode_b64_field(
                    image.get("data_b64"),
                    label="image.data_b64",
                    max_decoded_bytes=MAX_IMAGE_BYTES,
                )
            return app.feed.post_observed(
                project_id=project_id,
                handle=_required_text(payload, "handle"),
                text=_required_text(payload, "text"),
                image_path=image_path,
                image_bytes=image_bytes,
                url=payload.get("url"),
                ref=payload.get("ref"),
            )

    if sync_targets_source is not None:

        @http.get("/api/daemon/sync-targets")
        def daemon_sync_targets(
            request: Request, client_id: str = Query("")  # noqa: ARG001
        ) -> dict[str, Any]:
            principal = require_daemon(request)
            targets = sync_targets_source.sync_targets(
                tenant_id=getattr(principal, "tenant_id", "") or ""
            )
            return {
                "targets": [
                    {
                        "experiment_id": str(t["row"].get("experiment_id") or ""),
                        "row": t["row"],
                        "session": t["session"],
                    }
                    for t in targets
                ]
            }
