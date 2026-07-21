from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fastapi import Request

from merv.brain.kernel.utils import DataPlaneRequiredError, NotFoundError
from merv.brain.surface.identity import Principal
from merv.brain.surface.transport.api.gateway import (
    ProjectAuthorizer,
    ToolInvocationGateway,
)
from merv.brain.surface.transport.http_policy import HttpSurfacePolicy


USER = Principal(tenant_id="local", client_id="test", user_id="user-a")


def _request(path: str, *, query: str = "", principal=USER) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    request.state.principal = principal
    return request


class _Backend:
    def __init__(self, *, review_project_id: str = "proj-a") -> None:
        self.calls: list[dict] = []
        self.reviews = SimpleNamespace(
            request_project_id=lambda **_kwargs: review_project_id
        )

    def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


class HttpGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lookups: list[tuple[str, str]] = []

        def member_lookup(*, project_id: str, user_id: str) -> bool:
            self.lookups.append((project_id, user_id))
            return project_id == "proj-a" and user_id == USER.user_id

        self.projects = ProjectAuthorizer(member_lookup=member_lookup)
        self.surface = HttpSurfacePolicy.for_surface(
            restrict_cors=True, hosted_control=True
        )

    def test_one_authorizer_covers_path_query_tool_and_data_plane_scopes(self) -> None:
        self.assertIsNone(
            self.projects.http_denial(_request("/api/projects/proj-a/home"))
        )
        denied = self.projects.http_denial(_request("/api/projects/proj-b/home"))
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(
            json.loads(denied.body),
            {"detail": "project not found", "error_code": "not_found"},
        )
        missing = self.projects.http_denial(_request("/api/activity"))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(
            json.loads(missing.body)["detail"],
            "project_id is required on this endpoint when authenticated",
        )

        backend = _Backend()
        gateway = ToolInvocationGateway(
            backend=backend, surface=self.surface, projects=self.projects
        )
        self.assertEqual(
            gateway.call(
                name="claim.list", arguments={"project_id": "proj-a"}, principal=USER
            ),
            {"ok": True},
        )
        with self.assertRaisesRegex(NotFoundError, "project not found: proj-b"):
            gateway.call(
                name="claim.list", arguments={"project_id": "proj-b"}, principal=USER
            )
        with self.assertRaisesRegex(NotFoundError, "project not found: proj-b"):
            gateway.app_for_data_plane_project(_request("/api/data-plane/x"), "proj-b")

    def test_indirect_review_scope_uses_the_same_membership_boundary(self) -> None:
        denied_gateway = ToolInvocationGateway(
            backend=_Backend(review_project_id="proj-b"),
            surface=self.surface,
            projects=self.projects,
        )
        with self.assertRaisesRegex(NotFoundError, "project not found: proj-b"):
            denied_gateway.call(
                name="review.start",
                arguments={"review_request_id": "req-1"},
                principal=USER,
            )

        backend = _Backend()
        gateway = ToolInvocationGateway(
            backend=backend, surface=self.surface, projects=self.projects
        )
        gateway.call(
            name="review.start",
            arguments={"review_request_id": "req-1"},
            principal=USER,
        )
        self.assertEqual(backend.calls[0]["telemetry_project_id"], "proj-a")

    def test_project_listing_passes_authenticated_user_as_internal_context(
        self,
    ) -> None:
        backend = _Backend()
        gateway = ToolInvocationGateway(
            backend=backend, surface=self.surface, projects=self.projects
        )
        gateway.call(name="project.list", principal=USER)
        self.assertEqual(backend.calls[0]["internal_kwargs"], {"user_id": USER.user_id})

    def test_catalog_plane_allows_hybrid_project_but_rejects_local_data_tools(
        self,
    ) -> None:
        backend = _Backend()
        gateway = ToolInvocationGateway(
            backend=backend, surface=self.surface, projects=self.projects
        )
        gateway.call(
            name="project",
            arguments={"action": "create", "name": "A project"},
            principal=USER,
        )
        self.assertEqual(backend.calls[0]["name"], "project")
        with self.assertRaises(DataPlaneRequiredError):
            gateway.call(
                name="resource.register",
                arguments={"project_id": "proj-a", "path": "plan.md"},
                principal=USER,
            )


if __name__ == "__main__":
    unittest.main()
