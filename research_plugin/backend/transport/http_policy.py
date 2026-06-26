"""HTTP surface policy independent of FastAPI route wiring."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostedToolPolicy:
    telemetry_from_review_request: bool = False


@dataclass(frozen=True)
class HttpSurfacePolicy:
    restrict_cors: bool
    hosted_control: bool
    expose_local_data_plane: bool
    accept_repo_root_context: bool
    allow_data_plane_http: bool
    allow_data_plane_tool_calls: bool
    use_hosted_tool_policies: bool

    @classmethod
    def for_surface(
        cls,
        *,
        restrict_cors: bool,
        hosted_control: bool,
        expose_local_data_plane: bool,
    ) -> "HttpSurfacePolicy":
        return cls(
            restrict_cors=restrict_cors,
            hosted_control=hosted_control,
            expose_local_data_plane=expose_local_data_plane,
            accept_repo_root_context=expose_local_data_plane,
            allow_data_plane_http=expose_local_data_plane,
            allow_data_plane_tool_calls=expose_local_data_plane,
            use_hosted_tool_policies=hosted_control,
        )

    def data_plane_http_capabilities(self) -> dict[str, bool]:
        return {
            feature: self.allow_data_plane_http
            for feature in HTTP_DATA_PLANE_FEATURE_TO_TOOL
        }


HOSTED_CONTROL_TOOL_POLICIES = {
    "project.create": HostedToolPolicy(),
    "project.list": HostedToolPolicy(),
    "project.current": HostedToolPolicy(),
    "review.start": HostedToolPolicy(telemetry_from_review_request=True),
}


# Browser-visible /api/meta capability keys for local data-plane HTTP routes.
# Daemon-only data-plane endpoints use daemon auth instead of this UI handshake.
HTTP_DATA_PLANE_FEATURE_TO_TOOL = {
    "resource_registration": "resource.register_file",
    "resource_association": "resource.associate",
}
