"""HTTP surface policy independent of FastAPI route wiring."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostedToolPolicy:
    tenant_id_fallback: str | None = ""
    telemetry_from_review_request: bool = False


@dataclass(frozen=True)
class HttpSurfacePolicy:
    require_bearer_auth: bool
    restrict_cors: bool
    hosted_control: bool
    expose_local_data_plane: bool
    accept_repo_root_context: bool
    allow_data_plane_http: bool
    allow_data_plane_tool_calls: bool
    use_hosted_tool_policies: bool
    enforce_project_scope: bool
    release_uses_final_pull: bool

    @classmethod
    def for_surface(
        cls,
        *,
        require_bearer_auth: bool,
        restrict_cors: bool,
        hosted_control: bool,
        expose_local_data_plane: bool,
    ) -> "HttpSurfacePolicy":
        return cls(
            require_bearer_auth=require_bearer_auth,
            restrict_cors=restrict_cors,
            hosted_control=hosted_control,
            expose_local_data_plane=expose_local_data_plane,
            accept_repo_root_context=expose_local_data_plane,
            allow_data_plane_http=expose_local_data_plane,
            allow_data_plane_tool_calls=expose_local_data_plane,
            use_hosted_tool_policies=hosted_control,
            enforce_project_scope=require_bearer_auth,
            release_uses_final_pull=expose_local_data_plane,
        )

    def data_plane_http_capabilities(self) -> dict[str, bool]:
        return {
            feature: self.allow_data_plane_http
            for feature in HTTP_DATA_PLANE_FEATURE_TO_TOOL
        }


HOSTED_CONTROL_TOOL_POLICIES = {
    "project.create": HostedToolPolicy(tenant_id_fallback=None),
    "project.list": HostedToolPolicy(),
    "project.current": HostedToolPolicy(),
    "review.start": HostedToolPolicy(telemetry_from_review_request=True),
}


# Browser-visible /api/meta capability keys for local data-plane HTTP routes.
# Daemon-only data-plane endpoints use daemon auth instead of this UI handshake.
HTTP_DATA_PLANE_FEATURE_TO_TOOL = {
    "resource_registration": "resource.register_file",
    "resource_association": "resource.associate",
    "sandbox_sync": "sandbox.sync",
}
