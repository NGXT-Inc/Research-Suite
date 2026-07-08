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
    allow_data_plane_http: bool
    use_hosted_tool_policies: bool

    @classmethod
    def for_surface(
        cls,
        *,
        restrict_cors: bool,
        hosted_control: bool,
    ) -> "HttpSurfacePolicy":
        return cls(
            restrict_cors=restrict_cors,
            hosted_control=hosted_control,
            allow_data_plane_http=False,
            use_hosted_tool_policies=hosted_control,
        )

    def data_plane_http_capabilities(self) -> dict[str, bool]:
        return {
            feature: False
            for feature in HTTP_DATA_PLANE_FEATURE_TO_TOOL
        }


HOSTED_CONTROL_TOOL_POLICIES = {
    "project.create": HostedToolPolicy(),
    "project.list": HostedToolPolicy(),
    "project.current": HostedToolPolicy(),
    "review.start": HostedToolPolicy(telemetry_from_review_request=True),
}


# Browser-visible /api/meta capability keys for local data-plane HTTP routes.
# Proxy submission endpoints are not browser UI capabilities. Register and
# associate are one tool now (resource.register), but the two browser
# capability keys stay distinct so the UI's feature surface is unchanged.
HTTP_DATA_PLANE_FEATURE_TO_TOOL = {
    "resource_registration": "resource.register",
    "resource_association": "resource.register",
}
