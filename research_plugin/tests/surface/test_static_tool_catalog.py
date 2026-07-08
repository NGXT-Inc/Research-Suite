"""The checked-in tool catalog keeps the proxy runnable on bare python3.

mcp_server/_tool_catalog.json is the pydantic-free fallback for
tools/list on client machines with no pip installs. Two guarantees pin it:
the file is byte-identical to the live contracts rendering (regenerate with
scripts/regen_tool_catalog.py), and the proxy actually serves the same
catalog when pydantic cannot be imported.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

from backend.tools.contracts import STORAGE_TOOL_NAMES, TOOL_CONTRACTS
from mcp_server.local_data_plane import LocalDataPlane
from mcp_server.proxy import (
    _STATIC_CATALOG_PATH,
    _render_static_catalog_text,
    HttpProxyMcpServer,
    ProxyConfig,
)


class _BlockThirdParty:
    """Meta-path finder that makes pydantic unimportable."""

    _BLOCKED = ("pydantic", "pydantic_core")

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ARG002
        if name.split(".")[0] in self._BLOCKED:
            raise ImportError(f"blocked for bare-python test: {name}")
        return None


@contextlib.contextmanager
def _without_pydantic():
    """Simulate a machine where pydantic was never installed.

    Evicts cached backend/pydantic modules so re-imports actually execute,
    and blocks pydantic at the finder so those re-imports fail the same way
    they would on a bare python3.
    """
    prefixes = ("backend", "pydantic")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".")[0] in prefixes
    }
    for name in saved:
        del sys.modules[name]
    finder = _BlockThirdParty()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in [n for n in sys.modules if n.split(".")[0] in prefixes]:
            del sys.modules[name]
        sys.modules.update(saved)


class StaticCatalogParityTest(unittest.TestCase):
    def test_checked_in_catalog_matches_live_contracts(self) -> None:
        self.assertEqual(
            _STATIC_CATALOG_PATH.read_text(encoding="utf-8"),
            _render_static_catalog_text(),
            "mcp_server/_tool_catalog.json is stale — run "
            "scripts/regen_tool_catalog.py after changing tool contracts.",
        )

    def test_storage_tools_are_exactly_the_storage_prefix(self) -> None:
        # The bare-python catalog reader drops storage tools by name prefix;
        # pin the prefix convention so the two filters cannot diverge.
        self.assertEqual(
            STORAGE_TOOL_NAMES,
            {name for name in TOOL_CONTRACTS if name.startswith("storage.")},
        )


class BarePythonProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def _tools_list(self) -> list[dict]:
        # Brain down on purpose: tools/list must still serve the local half.
        proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=self.repo, control_url="http://127.0.0.1:1")
        )
        response = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertNotIn("error", response, response)
        return response["result"]["tools"]

    def test_tools_list_without_pydantic_matches_live_catalog(self) -> None:
        live = self._tools_list()
        with _without_pydantic():
            bare = self._tools_list()

        self.assertEqual(bare, live)
        names = {tool["name"] for tool in bare}
        self.assertIn("resource.register", names)
        # The bare/offline catalog is the data-plane half only. The merged
        # `project` tool is control-plane (brain-served), so with the brain
        # unreachable it is not listed offline — a documented consequence of the
        # merge (connect was cloud-validated anyway).
        self.assertNotIn("project", names)
        self.assertNotIn("project.connect", names)

    def test_sandbox_request_without_pydantic_skips_local_validation(self) -> None:
        captured: dict = {}

        def control_api_post(path: str, payload: dict) -> dict:
            captured["path"] = path
            captured["payload"] = payload
            return {"ok": True}

        plane = LocalDataPlane(
            repo_root=self.repo,
            project_id_resolver=lambda: "proj_bare",
            control_api_post=control_api_post,
            control_tool_call=lambda tool, args: {},
        )
        with _without_pydantic():
            result = plane.call_tool(
                name="sandbox.request",
                arguments={"public_key": "ssh-ed25519 AAAA bare-client"},
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["path"], "/api/data-plane/sandboxes/request")
        self.assertEqual(captured["payload"]["project_id"], "proj_bare")


if __name__ == "__main__":
    unittest.main()
