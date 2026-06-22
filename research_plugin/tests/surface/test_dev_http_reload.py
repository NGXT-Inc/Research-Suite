from __future__ import annotations

import importlib.util
import socket
import unittest
from pathlib import Path

from tests.paths import PLUGIN_ROOT


def load_dev_http_reload():
    script = PLUGIN_ROOT / "scripts" / "dev_http_reload.py"
    spec = importlib.util.spec_from_file_location("dev_http_reload", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class DevHttpReloadTest(unittest.TestCase):
    def test_port_in_use_detects_listener(self) -> None:
        module = load_dev_http_reload()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            self.assertTrue(module.port_in_use("127.0.0.1", port))

    def test_port_zero_is_never_reported_in_use(self) -> None:
        module = load_dev_http_reload()
        self.assertFalse(module.port_in_use("127.0.0.1", 0))

    def test_server_command_defaults_to_shared_mode(self) -> None:
        module = load_dev_http_reload()
        command = module.server_command(
            launcher=Path("/tmp/bin/research-plugin-http"),
            registry_store_path=Path("/tmp/registry.sqlite"),
            host="127.0.0.1",
            port=8787,
            activity_stderr=True,
        )
        self.assertIn("--registry-store", command)
        self.assertNotIn("--repo", command)
        self.assertNotIn("--store", command)
        self.assertIn("--activity-stderr", command)


if __name__ == "__main__":
    unittest.main()
