from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fastapi import Request

from merv.brain.kernel.utils import DataPlaneRequiredError, NotFoundError
from merv.brain.surface.identity import Principal, ProjectKeyScopeError
from merv.brain.surface.transport.api.gateway import (
    ProjectAuthorizer,
    ToolInvocationGateway,
)
from merv.brain.surface.transport.http_policy import HttpSurfacePolicy


USER = Principal(tenant_id="local", client_id="test", user_id="user-a")
# A project (mk_) key bound to proj-a; its owner is a member of proj-a.
KEY = Principal(
    tenant_id="local",
    client_id="project-key:k1",
    user_id="user-a",
    key_id="k1",
    key_project_id="proj-a",
)
_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 a@b"


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
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


class HttpGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lookups: list[tuple[str, str]] = []

        def member_lookup(*, project_id: str, user_id: str) -> bool:
            self.lookups.append((project_id, user_id))
            return project_id == "proj-a" and user_id == USER.user_id

        self.projects = ProjectAuthorizer(
            projects=SimpleNamespace(is_member=member_lookup)
        )
        self.surface = HttpSurfacePolicy.for_surface(
            restrict_cors=True, hosted_control=True
        )

    def gateway(
        self, backend: _Backend | None = None, *, review_project_id: str = "proj-a"
    ) -> ToolInvocationGateway:
        return ToolInvocationGateway(
            tools=backend or _Backend(),
            reviews=SimpleNamespace(
                request_project_id=lambda **_kwargs: review_project_id
            ),
            sandboxes=SimpleNamespace(get=lambda **_kwargs: {"ok": True}),
            surface=self.surface,
            projects=self.projects,
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
        gateway = self.gateway(backend)
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
            gateway.authorize_data_plane_project(
                _request("/api/data-plane/x"), "proj-b"
            )

    def test_indirect_review_scope_uses_the_same_membership_boundary(self) -> None:
        denied_gateway = self.gateway(review_project_id="proj-b")
        with self.assertRaisesRegex(NotFoundError, "project not found: proj-b"):
            denied_gateway.call(
                name="review.start",
                arguments={"review_request_id": "req-1"},
                principal=USER,
            )

        backend = _Backend()
        gateway = self.gateway(backend)
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
        gateway = self.gateway(backend)
        gateway.call(name="project.list", principal=USER)
        self.assertEqual(backend.calls[0]["internal_kwargs"], {"user_id": USER.user_id})

    def test_catalog_plane_allows_hybrid_project_but_rejects_local_data_tools(
        self,
    ) -> None:
        backend = _Backend()
        gateway = self.gateway(backend)
        gateway.call(
            name="project",
            arguments={"action": "create", "name": "A project"},
            principal=USER,
        )
        self.assertEqual(backend.calls[0]["name"], "project")
        with self.assertRaises(DataPlaneRequiredError):
            gateway.call(
                name="storage.upload_file",
                arguments={"project_id": "proj-a", "path": "model.bin", "kind": "model"},
                principal=USER,
            )


class _Sandboxes:
    """Fake SandboxFacade recording the control-path calls a key makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"ok": True}

    def request(self, **kwargs):
        self.calls.append(("request", kwargs))
        return {"status": "running"}

    def attach(self, **kwargs):
        self.calls.append(("attach", kwargs))
        return {"status": "running"}

    def pull_outputs_command(self, **kwargs):
        self.calls.append(("pull_outputs_command", kwargs))
        return {"rsync": "rsync ..."}


class KeySandboxControlPathTest(unittest.TestCase):
    """Phase C: an mk_ key reaches the sandbox data-plane surface over control."""

    def setUp(self) -> None:
        def member_lookup(*, project_id: str, user_id: str) -> bool:
            return project_id == "proj-a" and user_id == "user-a"

        self.projects = ProjectAuthorizer(
            projects=SimpleNamespace(is_member=member_lookup)
        )
        self.sandboxes = _Sandboxes()
        self.gateway = ToolInvocationGateway(
            tools=_Backend(),
            reviews=SimpleNamespace(request_project_id=lambda **_k: "proj-a"),
            sandboxes=self.sandboxes,
            surface=HttpSurfacePolicy.for_surface(
                restrict_cors=True, hosted_control=True
            ),
            projects=self.projects,
        )

    def test_key_principal_is_served_request_over_control(self) -> None:
        result = self.gateway.call(
            name="sandbox.request",
            arguments={"project_id": "proj-a", "public_key": _PUBKEY, "gpu": "A100"},
            principal=KEY,
        )
        self.assertEqual(result, {"status": "running"})
        name, kwargs = self.sandboxes.calls[-1]
        self.assertEqual(name, "request")
        # Plain control path: no local_dir enrichment; key attribution flows.
        self.assertFalse(kwargs["include_data_plane_enrichment"])
        self.assertEqual(kwargs["provisioning_key_id"], "k1")
        self.assertEqual(kwargs["provisioning_user_id"], "user-a")
        self.assertEqual(kwargs["project_id"], "proj-a")

    def test_key_principal_attach_and_pull_outputs_are_served(self) -> None:
        self.gateway.call(
            name="sandbox.attach",
            arguments={
                "project_id": "proj-a",
                "experiment_id": "exp1",
                "sandbox_uid": "uid1",
            },
            principal=KEY,
        )
        self.assertEqual(self.sandboxes.calls[-1][0], "attach")
        # attach does NOT install the caller's key — no public_key is forwarded.
        self.assertNotIn("public_key", self.sandboxes.calls[-1][1])
        self.assertNotIn("public_key_override", self.sandboxes.calls[-1][1])
        self.gateway.call(
            name="sandbox.pull_outputs",
            arguments={"project_id": "proj-a", "sandbox_uid": "uid1"},
            principal=KEY,
        )
        self.assertEqual(self.sandboxes.calls[-1][0], "pull_outputs_command")

    def test_key_principal_cannot_reach_a_different_project(self) -> None:
        with self.assertRaises(ProjectKeyScopeError):
            self.gateway.call(
                name="sandbox.request",
                arguments={"project_id": "proj-b", "public_key": _PUBKEY},
                principal=KEY,
            )
        self.assertEqual(self.sandboxes.calls, [])

    def test_non_key_principal_keeps_the_local_data_plane_path(self) -> None:
        # A raw JWT (no key_id) is NOT served over control — the local proxy
        # still owns sandbox.request until Phase D.
        with self.assertRaises(DataPlaneRequiredError):
            self.gateway.call(
                name="sandbox.request",
                arguments={"project_id": "proj-a", "public_key": _PUBKEY},
                principal=USER,
            )
        self.assertEqual(self.sandboxes.calls, [])


if __name__ == "__main__":
    unittest.main()
