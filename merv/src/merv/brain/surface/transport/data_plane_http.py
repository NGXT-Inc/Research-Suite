"""Control-side HTTP endpoints used by stateless local data-plane proxies."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any

from fastapi import Body, Request
from merv.shared.tool_validation import validate_openssh_public_key

from ...feed.feed import MAX_EMBED_BYTES, MAX_IMAGE_BYTES
from ...kernel.utils import ValidationError

JsonBody = dict[str, Any] | None
DataPlaneProjectApp = Callable[[Request, str], Any]


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


def register_data_plane_routes(
    http: Any,
    *,
    app_for_project: DataPlaneProjectApp,
) -> None:
    @http.post("/api/data-plane/resources/validate-association")
    def data_plane_validate_resource_association(
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

    @http.post("/api/data-plane/resources/observe")
    def data_plane_observe_resource(
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
            content_type=str(payload.get("content_type") or "application/octet-stream"),
        )

    @http.post("/api/data-plane/resources/associate")
    def data_plane_associate_resource(
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

    @http.post("/api/data-plane/feed/validate-post")
    def data_plane_validate_feed_post(
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
            kind=payload.get("kind"),
            in_reply_to=payload.get("in_reply_to"),
        )

    @http.post("/api/data-plane/sandboxes/request")
    def data_plane_request_sandbox(
        request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        payload = body or {}
        project_id = _required_text(payload, "project_id")
        public_key = validate_openssh_public_key(_required_text(payload, "public_key"))
        if not public_key:
            raise ValidationError("public_key is required for sandbox.request")
        experiment_id = str(payload.get("experiment_id") or "").strip()
        app = app_for_project(request, project_id)
        return app.sandboxes.request_from_data_plane(
            project_id=project_id,
            experiment_id=experiment_id,
            public_key=public_key,
            gpu=payload.get("gpu"),
            cpu=payload.get("cpu"),
            memory=payload.get("memory"),
            time_limit=payload.get("time_limit"),
            instance_type=payload.get("instance_type"),
            region=payload.get("region"),
            provider=payload.get("provider"),
            additional=bool(payload.get("additional")),
            sandbox_uid=payload.get("sandbox_uid"),
        )

    @http.post("/api/data-plane/sandboxes/attach")
    def data_plane_attach_sandbox(
        request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        payload = body or {}
        project_id = _required_text(payload, "project_id")
        experiment_id = _required_text(payload, "experiment_id")
        sandbox_uid = _required_text(payload, "sandbox_uid")
        app = app_for_project(request, project_id)
        return app.sandboxes.attach_from_data_plane(
            project_id=project_id,
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid,
            public_key=str(payload.get("public_key") or ""),
        )

    @http.post("/api/data-plane/feed/post")
    def data_plane_post_feed(
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
            kind=payload.get("kind"),
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
        html = payload.get("html")
        html_bytes = None
        html_path = None
        if html is not None:
            if not isinstance(html, dict):
                raise ValidationError("html must be an object")
            html_path = str(html.get("path") or "feed-embed")
            html_bytes = _decode_b64_field(
                html.get("data_b64"),
                label="html.data_b64",
                max_decoded_bytes=MAX_EMBED_BYTES,
            )
        return app.feed.post_observed(
            project_id=project_id,
            handle=_required_text(payload, "handle"),
            text=_required_text(payload, "text"),
            image_path=image_path,
            image_bytes=image_bytes,
            html_path=html_path,
            html_bytes=html_bytes,
            url=payload.get("url"),
            ref=payload.get("ref"),
            kind=payload.get("kind"),
            in_reply_to=payload.get("in_reply_to"),
        )
