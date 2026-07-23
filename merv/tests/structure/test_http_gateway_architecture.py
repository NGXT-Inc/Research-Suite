from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.paths import RESEARCH_CORE_ROOT, SURFACE_ROOT


API = SURFACE_ROOT / "transport" / "api"
APP = API / "app.py"
GATEWAY = API / "gateway.py"


class HttpGatewayArchitectureTest(unittest.TestCase):
    def test_factory_is_composition_only(self) -> None:
        source = APP.read_text(encoding="utf-8")
        # +3 for no-dataplane Phase C wiring: the /api/user/hf-token router and
        # the MCP-catalog clause that advertises the key-sandbox surface. The
        # serving LOGIC lives in sandbox_control.py, not here.
        self.assertLessEqual(len(source.splitlines()), 128)
        for seam in (
            "RequestAuthenticator",
            "ProjectAuthorizer",
            "ToolInvocationGateway",
            "install_request_middleware",
            "install_activity_middleware",
            "install_cors",
        ):
            self.assertIn(seam, source)
        for escaped_detail in (
            "verify_bearer",
            "is_project_member",
            "TOOL_MANIFEST",
            "HOSTED_CONTROL_TOOL_POLICIES",
            "CORSMiddleware",
            '@http.middleware("http")',
            "re.compile",
        ):
            self.assertNotIn(escaped_detail, source)
        factory = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "create_fastapi_app"
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(factory)
                if node is not factory
            )
        )

    def test_gateway_is_smaller_than_the_factory_logic_it_replaced(self) -> None:
        app_loc = len(APP.read_text(encoding="utf-8").splitlines())
        gateway_loc = len(GATEWAY.read_text(encoding="utf-8").splitlines())
        # +17 for the no-dataplane Phase C key-sandbox control path: only the
        # import + the dispatch branch land here; the serving logic is factored
        # into sandbox_control.py so the request-aware gateway stays focused.
        self.assertLessEqual(gateway_loc, 392)
        self.assertLessEqual(app_loc + gateway_loc, 520)

    def test_project_membership_has_one_transport_lookup(self) -> None:
        package_source = "\n".join(
            path.read_text(encoding="utf-8") for path in API.glob("*.py")
        )
        gateway_source = GATEWAY.read_text(encoding="utf-8")
        projects_source = (RESEARCH_CORE_ROOT / "projects.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("is_project_member", package_source)
        self.assertEqual(gateway_source.count("self.projects.is_member("), 2)
        self.assertIn("ProjectAuthorizer(projects=api.projects)", APP.read_text())
        self.assertIn("def is_member(", projects_source)

    def test_gateway_names_the_three_public_boundaries(self) -> None:
        source = GATEWAY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertEqual(
            classes,
            {"RequestAuthenticator", "ProjectAuthorizer", "ToolInvocationGateway"},
        )
        self.assertIn("TOOL_MANIFEST.get(name)", source)
        self.assertNotIn("DATA_PLANE_TOOL_NAMES", source)
        self.assertNotIn("PROJECT_SCOPED_TOOL_NAMES", source)

    def test_route_modules_cannot_bypass_the_request_aware_gateway(self) -> None:
        for path in API.glob("*.py"):
            if path.name in {"context.py", "gateway.py", "views.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("api.call_tool(", source, path.name)
            self.assertNotIn("**(body or {})", source, path.name)

    def test_route_names_cannot_shadow_imports_or_injected_dependencies(self) -> None:
        conflicts: dict[str, list[str]] = {}
        for path in API.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = {
                alias.asname or alias.name.split(".")[0]
                for node in tree.body
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                alias.asname or alias.name
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
                if alias.name != "*"
            }
            router = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "build_router"
                ),
                None,
            )
            if router is None:
                continue
            parameters = {
                argument.arg
                for argument in (
                    *router.args.posonlyargs,
                    *router.args.args,
                    *router.args.kwonlyargs,
                )
            }
            parameters.update(
                argument.arg
                for argument in (router.args.vararg, router.args.kwarg)
                if argument is not None
            )
            nested_names = {
                node.name
                for node in router.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            shadowed = sorted(nested_names & (imports | parameters))
            if shadowed:
                conflicts[path.name] = shadowed
        self.assertEqual({}, conflicts)


if __name__ == "__main__":
    unittest.main()
