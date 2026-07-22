"""Shrinking inventory of untyped cross-component collaborators."""

from __future__ import annotations

import ast
import inspect
import unittest
from collections import Counter
from pathlib import Path
from types import NoneType
from typing import get_args, get_type_hints

from tests.paths import BACKEND_ROOT


_BOOTSTRAP_FILES = {
    "sandbox/execution/__init__.py",
    "sandbox/execution/driver_registry.py",
    "surface/config.py",
    "surface/control/control_app.py",
    "surface/control/record_core.py",
    "surface/transport/http_server.py",
}


def _is_bootstrap(rel: str) -> bool:
    return rel in _BOOTSTRAP_FILES or rel.startswith("surface/composition/")


def _debt(lines: str) -> Counter[tuple[str, str, str, str]]:
    return Counter(tuple(line.split(" | ", 3)) for line in lines.splitlines() if line)


DEPENDENCY_TYPE_DEBT = _debt(
    """application/queries.py | ExperimentFigureQuery | experiment_state | RecordQuery
application/queries.py | ExperimentFigureQuery | review_snapshot | RecordQuery
application/queries.py | ExperimentFigureQuery | open_reviews | RecordsQuery
application/queries.py | ExperimentFigureQuery | sandbox_row | Callable[..., Record | None]
application/queries.py | ExperimentFigureQuery | sandbox_view | RecordQuery
application/queries.py | ExperimentFigureQuery | sandbox_status_active | Callable[[str], bool]
application/queries.py | TenantCountersQuery | event_count | Callable[..., int]
application/queries.py | TenantCountersQuery | generation_counters | RecordQuery
application/queries.py | ComputeCostQuery | project_spend | RecordQuery
application/workflow.py | ProjectDashboardQuery | resources | RecordQuery
application/workflow.py | ProjectDashboardQuery | review_queue | RecordQuery
application/workflow.py | ProjectDashboardQuery | recent_events | RecordQuery
application/workflow.py | ProjectDashboardQuery | health | Callable[[], dict[str, object]]
application/workflow.py | ProjectDashboardQuery | current | RecordQuery
sandbox/facade.py | SandboxFacade.__init__ | attachment_check | Callable[..., None] | None
kernel/state/dialects.py | PostgresConnection.__init__ | raw | Any
mlflow/tracking.py | CentralMlflowService.__init__ | health_check | Callable[[], bool] | None
object_storage/s3_blobs.py | S3BlobStore.__init__ | client | Any | None
object_storage/s3_object_store.py | S3CompatibleObjectStore.__init__ | client | Any | None
sandbox/execution/backends/modal/sandbox_backend.py | ModalSandboxBackend.__init__ | modal_module | Any | None
sandbox/execution/backends/modal/sandbox_backend.py | ModalSandboxBackend.__init__ | activity | ActivityHook | None
sandbox/execution/backends/modal/sandbox_backend.py | build_modal_sandbox_backend | activity | ActivityHook | None
sandbox/execution/backends/thunder_compute/sandbox_backend.py | ThunderComputeSandboxBackend.__init__ | bootstrap_runner | BootstrapRunner | None
sandbox/sandbox_daemons.py | SandboxDaemons.__init__ | sample_metrics | Callable[..., dict[str, Any]] | None
sandbox/sandbox_daemons.py | SandboxDaemons.__init__ | reconcile_runs | Callable[[], int] | None
sandbox/sandbox_heartbeat.py | SandboxHeartbeatMonitor.__init__ | repository | Any
sandbox/sandbox_heartbeat.py | SandboxHeartbeatMonitor.__init__ | sample_metrics | Callable[..., dict[str, Any]]
sandbox/sandbox_heartbeat.py | SandboxHeartbeatMonitor.__init__ | reap_row | Callable[..., None]
sandbox/transcript_cache.py | TranscriptCache.__init__ | clock | Callable[[], float] | None
surface/observability.py | StructuredLogger.__init__ | stream | Any | None
surface/transport/admin_http.py | register_admin_routes | cleanup | Any | None
surface/transport/admin_http.py | register_admin_routes | tenant_counters | Any | None
surface/transport/api/context.py | ApiRouteContext | route_call_tool | Callable[..., dict[str, Any]]
surface/transport/api/gateway.py | RequestAuthenticator | verifier | Any | None
surface/transport/api/sdk_auth.py | build_router | verifier | Any
surface/transport/mcp_http.py | register_mcp_routes | list_tools | ToolCatalog
surface/transport/mcp_http.py | register_mcp_routes | call_tool | ToolCaller
surface/transport/mcp_http.py | register_mcp_routes | allow_tool | ToolFilter | None
surface/transport/mcp_http.py | register_mcp_routes | authorize | Authorizer | None"""
)


def _contains_name(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node))


def _bare_any(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "Any"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _bare_any(node.left) or _bare_any(node.right)
    return False


def _callable_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in tree.body:
            if not (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                continue
            if _contains_name(node.value, {"Callable", *aliases}):
                name = node.targets[0].id
                if name not in aliases:
                    aliases.add(name)
                    changed = True
    return aliases


def _is_untyped_dependency(annotation: ast.AST, aliases: set[str]) -> bool:
    return _bare_any(annotation) or _contains_name(
        annotation, {"Callable", *aliases}
    )


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return [
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ]


def _dependency_type_debt() -> Counter[tuple[str, str, str, str]]:
    debt: Counter[tuple[str, str, str, str]] = Counter()
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _callable_aliases(tree)

        if _is_bootstrap(rel):
            continue
        for owner in tree.body:
            if isinstance(owner, ast.ClassDef):
                for field in owner.body:
                    if (
                        isinstance(field, ast.AnnAssign)
                        and isinstance(field.target, ast.Name)
                        and _is_untyped_dependency(field.annotation, aliases)
                    ):
                        debt[(rel, owner.name, field.target.id, ast.unparse(field.annotation))] += 1
                init_name = f"{owner.name}.__init__"
                for method in owner.body:
                    if not (
                        isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name == "__init__"
                    ):
                        continue
                    for parameter in _parameters(method):
                        if parameter.annotation and _is_untyped_dependency(
                            parameter.annotation, aliases
                        ):
                            debt[(rel, init_name, parameter.arg, ast.unparse(parameter.annotation))] += 1
            elif (
                isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                and owner.name.startswith(("build_", "register_"))
            ):
                for parameter in _parameters(owner):
                    if (
                        parameter.arg != "http"
                        and parameter.annotation
                        and _is_untyped_dependency(
                        parameter.annotation, aliases
                        )
                    ):
                        debt[(rel, owner.name, parameter.arg, ast.unparse(parameter.annotation))] += 1
    return debt


def _format(counter: Counter[tuple[str, str, str, str]]) -> str:
    return ", ".join(
        f"{file}:{owner}.{name} ({annotation}) x{count}"
        for (file, owner, name, annotation), count in sorted(counter.items())
    )


class DependencyContractTest(unittest.TestCase):
    def test_control_manifest_methods_exist_on_declared_owner_contracts(self) -> None:
        from merv.brain.surface.tools.contracts import TOOL_MANIFEST
        from merv.brain.surface.tools.tool_handlers import build_control_tool_handlers

        hints = get_type_hints(build_control_tool_handlers)
        roots = {
            contract.handler_identity.split(".", 1)[0]
            for contract in TOOL_MANIFEST.values()
            if contract.plane == "control"
        }
        parameters = set(inspect.signature(build_control_tool_handlers).parameters)
        self.assertLessEqual(roots, parameters)
        for contract in TOOL_MANIFEST.values():
            if contract.plane != "control":
                continue
            root, method = contract.handler_identity.split(".", 1)
            annotation = hints[root]
            candidates = tuple(arg for arg in get_args(annotation) if arg is not NoneType)
            candidates = candidates or (annotation,)
            self.assertTrue(
                any(hasattr(candidate, method) for candidate in candidates),
                f"{contract.handler_identity} is absent from {annotation}",
            )

    def test_untyped_cross_component_dependency_inventory_only_shrinks(self) -> None:
        current = _dependency_type_debt()
        new = current - DEPENDENCY_TYPE_DEBT
        stale = DEPENDENCY_TYPE_DEBT - current
        self.assertFalse(
            new,
            "new Any/generic Callable collaborator; define a named Protocol: "
            + _format(new),
        )
        self.assertFalse(
            stale,
            "dependency typing improved; lower DEPENDENCY_TYPE_DEBT: "
            + _format(stale),
        )


if __name__ == "__main__":
    unittest.main()
