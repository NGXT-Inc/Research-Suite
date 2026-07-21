"""Checked-in proxy projections stay byte-identical to the live manifest.

src/merv/proxy/_tool_catalog.json serves tools/list on client machines with no
pip installs. Two guarantees pin it:
the file is byte-identical to the live contracts rendering (regenerate with
scripts/regen_tool_catalog.py), and the proxy actually serves the same
catalog when pydantic cannot be imported.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from merv.brain.surface.tools.contracts import STORAGE_TOOL_NAMES, TOOL_CONTRACTS
from merv.proxy.local_data_plane import LocalDataPlane, LocalDataPlaneError
from merv.proxy.proxy import (
    _STATIC_CATALOG_PATH,
    _PROXY_MANIFEST_PATH,
    _storage_feature_enabled,
    HttpProxyMcpServer,
    ProxyConfig,
)
from merv.shared.errors import ValidationError
from scripts.regen_tool_catalog import render_proxy_manifest_text, render_static_catalog_text


class _BlockBrainAndPydantic:
    """Meta-path finder that makes brain and pydantic imports fail."""

    _BLOCKED = ("merv.brain", "pydantic", "pydantic_core")

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ARG002
        if any(name == prefix or name.startswith(prefix + ".") for prefix in self._BLOCKED):
            raise ImportError(f"blocked for bare-python test: {name}")
        return None


@contextlib.contextmanager
def _without_brain_or_pydantic():
    """Simulate a bare client artifact with neither brain nor pydantic.

    Evicts cached src/merv/brain/pydantic modules so re-imports actually execute,
    and blocks pydantic at the finder so those re-imports fail the same way
    they would on a bare python3.
    """
    prefixes = ("merv.brain", "pydantic", "pydantic_core")

    def _matches(name: str) -> bool:
        return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)

    saved = {name: module for name, module in sys.modules.items() if _matches(name)}
    for name in saved:
        del sys.modules[name]
    finder = _BlockBrainAndPydantic()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for name in [n for n in sys.modules if _matches(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


class StaticCatalogParityTest(unittest.TestCase):
    def test_checked_in_catalog_matches_live_contracts(self) -> None:
        self.assertEqual(
            _STATIC_CATALOG_PATH.read_text(encoding="utf-8"),
            render_static_catalog_text(),
            "src/merv/proxy/_tool_catalog.json is stale — run "
            "scripts/regen_tool_catalog.py after changing tool contracts.",
        )
        self.assertEqual(
            _PROXY_MANIFEST_PATH.read_text(encoding="utf-8"),
            render_proxy_manifest_text(),
            "src/merv/proxy/_tool_manifest.json is stale — run "
            "scripts/regen_tool_catalog.py after changing tool contracts.",
        )

    def test_storage_feature_set_is_manifest_derived(self) -> None:
        self.assertEqual(
            STORAGE_TOOL_NAMES,
            {
                name
                for name, contract in TOOL_CONTRACTS.items()
                if "storage" in contract.feature_requirements
            },
        )

    def test_proxy_filters_features_without_tool_name_conventions(self) -> None:
        tools = (
            {
                "name": "blob.transfer",
                "description": "feature gated without a storage prefix",
                "inputSchema": {"type": "object"},
                "plane": "data",
                "executionStrategy": "local",
                "featureRequirements": ["storage"],
            },
            {
                "name": "storage.always",
                "description": "ungated despite a storage prefix",
                "inputSchema": {"type": "object"},
                "plane": "data",
                "executionStrategy": "local",
                "featureRequirements": [],
            },
        )
        proxy = HttpProxyMcpServer(
            config=ProxyConfig(repo_root=Path.cwd(), control_url="http://127.0.0.1:1")
        )
        with (
            patch("merv.proxy.tool_gateway.shipped_manifest", return_value=tools),
            patch("merv.proxy.tool_gateway.storage_feature_enabled", return_value=False),
        ):
            names = {tool["name"] for tool in proxy._local_tool_catalog()}
        self.assertEqual(names, {"storage.always"})

    def test_built_wheel_contains_both_proxy_manifests(self) -> None:
        package_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "package"
            wheel_dir = Path(tmp) / "wheel"
            staging.mkdir()
            wheel_dir.mkdir()
            for name in ("pyproject.toml", "README.md"):
                shutil.copy2(package_root / name, staging / name)
            shutil.copytree(
                package_root / "src",
                staging / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            env = dict(os.environ)
            setuptools_spec = importlib.util.find_spec("setuptools")
            if setuptools_spec is not None and setuptools_spec.origin:
                vendor = Path(setuptools_spec.origin).parent / "_vendor"
                if vendor.is_dir():
                    env["PYTHONPATH"] = os.pathsep.join((str(vendor), env.get("PYTHONPATH", "")))
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_dir),
                    ".",
                ],
                cwd=staging,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(built.returncode, 0, built.stderr or built.stdout)
            wheels = list(wheel_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                members = set(wheel.namelist())
        self.assertIn("merv/proxy/_tool_catalog.json", members)
        self.assertIn("merv/proxy/_tool_manifest.json", members)

    def test_proxy_storage_gate_matches_brain_dual_env_semantics(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_storage_feature_enabled())
        with patch.dict(os.environ, {"RESEARCH_PLUGIN_STORAGE_PROVIDER": " S3 "}, clear=True):
            self.assertTrue(_storage_feature_enabled())
        with patch.dict(
            os.environ,
            {
                "MERV_STORAGE_PROVIDER": " ",
                "RESEARCH_PLUGIN_STORAGE_PROVIDER": "s3",
            },
            clear=True,
        ):
            self.assertTrue(_storage_feature_enabled())
        with patch.dict(
            os.environ,
            {
                "MERV_STORAGE_PROVIDER": "S3",
                "RESEARCH_PLUGIN_STORAGE_PROVIDER": "invalid",
            },
            clear=True,
        ):
            self.assertTrue(_storage_feature_enabled())
        with patch.dict(
            os.environ, {"MERV_STORAGE_PROVIDER": "local"}, clear=True
        ), self.assertRaises(ValidationError):
            _storage_feature_enabled()


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

    def test_tools_list_without_brain_or_pydantic_matches_catalog(self) -> None:
        live = self._tools_list()
        with _without_brain_or_pydantic():
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

    def test_sandbox_request_without_brain_or_pydantic_uses_shared_validation(
        self,
    ) -> None:
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
        with _without_brain_or_pydantic():
            result = plane.call_tool(
                name="sandbox.request",
                arguments={"public_key": "ssh-ed25519 " + ("A" * 48) + " bare-client"},
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["path"], "/api/data-plane/sandboxes/request")
        self.assertEqual(captured["payload"]["project_id"], "proj_bare")

    def test_resource_register_without_brain_or_pydantic_uses_shared_validation(
        self,
    ) -> None:
        (self.repo / "result.json").write_text('{"score": 1}\n', encoding="utf-8")
        captured: dict = {}

        def control_api_post(path: str, payload: dict) -> dict:
            captured["path"] = path
            captured["payload"] = payload
            return {"id": "res_bare", **payload}

        plane = LocalDataPlane(
            repo_root=self.repo,
            project_id_resolver=lambda: "proj_bare",
            control_api_post=control_api_post,
            control_tool_call=lambda tool, args: {},
        )
        with _without_brain_or_pydantic():
            result = plane.call_tool(
                name="resource.register",
                arguments={"path": "result.json", "kind": "result"},
            )

        self.assertEqual(result["id"], "res_bare")
        self.assertEqual(captured["path"], "/api/data-plane/resources/observe")
        self.assertEqual(captured["payload"]["project_id"], "proj_bare")
        self.assertEqual(captured["payload"]["path"], "result.json")

    def test_resource_register_modes_are_checked_without_brain_or_pydantic(
        self,
    ) -> None:
        plane = LocalDataPlane(
            repo_root=self.repo,
            project_id_resolver=lambda: "proj_bare",
            control_api_post=lambda path, payload: {},
            control_tool_call=lambda tool, args: {},
        )
        with _without_brain_or_pydantic(), self.assertRaisesRegex(
            LocalDataPlaneError, "provide exactly one"
        ):
            plane.call_tool(
                name="resource.register",
                arguments={"path": "a.md", "paths": ["b.md"]},
            )


if __name__ == "__main__":
    unittest.main()
