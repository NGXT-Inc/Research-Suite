from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.client_cli import (
    HOSTED_CONTROL_URL,
    configure_client,
    main,
)
from backend.config import (
    CLIENT_CONFIG_ENV_VAR,
    CONTROL_URL_ENV_VAR,
    read_client_config,
    resolve_control_url,
    resolve_daemon_state_dir,
)
import mcp_server.__main__ as mcp_entrypoint
from mcp_server.__main__ import _repo_is_linked
from mcp_server.project_links import ProjectLinks


class ClientConfigTest(unittest.TestCase):
    def test_configure_writes_machine_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            config = configure_client(
                config_path=config_path,
                control_url="https://control.example.test/",
            )

            self.assertEqual(config["control_url"], "https://control.example.test")
            self.assertTrue(config_path.exists())
            self.assertEqual(
                read_client_config({CLIENT_CONFIG_ENV_VAR: str(config_path)})["control_url"],
                "https://control.example.test",
            )
            self.assertEqual(
                resolve_daemon_state_dir({CLIENT_CONFIG_ENV_VAR: str(config_path)}).resolve(),
                config_path.parent.resolve(),
            )
            self.assertEqual(
                resolve_control_url({CLIENT_CONFIG_ENV_VAR: str(config_path)}),
                "https://control.example.test",
            )

    def test_explicit_env_overrides_machine_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            configured = configure_client(
                config_path=config_path,
                control_url="https://configured.example.test",
            )

            env = {
                CLIENT_CONFIG_ENV_VAR: str(config_path),
                CONTROL_URL_ENV_VAR: "https://override.example.test",
            }
            self.assertEqual(resolve_control_url(env), "https://override.example.test")
            self.assertEqual(configured["control_url"], "https://configured.example.test")

    def test_configure_defaults_to_hosted_control_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            config = configure_client(
                config_path=config_path,
                control_url="",
            )

            self.assertEqual(config["control_url"], HOSTED_CONTROL_URL)
            self.assertNotIn("daemon_url", config)
            self.assertNotIn("daemon_secret_file", config)

    def test_connect_configures_and_links_without_daemon_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            repo = Path(tmp) / "repo"
            repo.mkdir()
            with patch("backend.client_cli.link_repo", return_value={"linked": True}) as link:
                with redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "--config",
                            str(config_path),
                            "connect",
                            "--control-url",
                            "https://control.example.test",
                            "--project-id",
                            "proj_123",
                            "--repo",
                            str(repo),
                        ]
                    )

            self.assertEqual(code, 0)
            self.assertTrue(config_path.exists())
            link.assert_called_once()
            self.assertEqual(link.call_args.kwargs["project_id"], "proj_123")
            self.assertEqual(link.call_args.kwargs["repo_root"], repo.resolve())

    def test_mcp_hosted_config_is_scoped_to_linked_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            linked = root / "linked"
            unlinked = root / "unlinked"
            linked.mkdir()
            unlinked.mkdir()
            links = ProjectLinks(db_path=root / "project_links.sqlite")
            links.link(repo_root=str(linked), project_id="proj_123")

            self.assertTrue(
                _repo_is_linked(db_path=root / "project_links.sqlite", repo_root=linked)
            )
            self.assertFalse(
                _repo_is_linked(db_path=root / "project_links.sqlite", repo_root=unlinked)
            )

    def test_mcp_launcher_uses_machine_transport_config_for_unlinked_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "client.json"
            unlinked = root / "unlinked"
            unlinked.mkdir()
            configure_client(
                config_path=config_path,
                control_url="https://control.example.test",
            )
            captured = {}

            class FakeProxy:
                def __init__(self, *, config):
                    captured["config"] = config

                def serve(self) -> None:
                    return None

            env = {
                CLIENT_CONFIG_ENV_VAR: str(config_path),
                "RESEARCH_PLUGIN_CONTROL_URL": "",
            }
            argv = [
                "merv-mcp",
                "--repo",
                str(unlinked),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "argv", argv),
                patch.object(mcp_entrypoint, "HttpProxyMcpServer", FakeProxy),
            ):
                self.assertEqual(mcp_entrypoint.main(), 0)

            proxy_config = captured["config"]
            self.assertEqual(proxy_config.control_url, "https://control.example.test")
            self.assertEqual(
                proxy_config.project_links_path.resolve(),
                (root / "project_links.sqlite").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
