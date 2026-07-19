from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from merv.brain.client_cli import link_repo, list_links, route_repo, unlink_repo
from merv.proxy.proxy import HttpProxyMcpServer, ProxyConfig


class ClientLinkingTest(unittest.TestCase):
    def test_client_links_many_folders_to_many_projects_in_proxy_link_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "client.json"
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()

            for repo, project_id in ((repo_a, "proj_a"), (repo_b, "proj_b")):
                linked = link_repo(
                    config_path=config_path,
                    repo_root=repo,
                    project_id=project_id,
                )
                self.assertTrue(linked["linked"])

            route_a = route_repo(config_path=config_path, repo_root=repo_a)
            route_b = route_repo(config_path=config_path, repo_root=repo_b)
            self.assertEqual(route_a["project_id"], "proj_a")
            self.assertEqual(route_b["project_id"], "proj_b")

            listed = list_links(config_path=config_path)
            got = {
                row["repo_root"]: row["project_id"]
                for row in listed["links"]
            }
            self.assertEqual(got[str(repo_a.resolve())], "proj_a")
            self.assertEqual(got[str(repo_b.resolve())], "proj_b")

            removed = unlink_repo(config_path=config_path, repo_root=repo_a)
            self.assertTrue(removed["unlinked"])
            route_a_after = route_repo(config_path=config_path, repo_root=repo_a)
            route_b_after = route_repo(config_path=config_path, repo_root=repo_b)
            self.assertFalse(route_a_after["exists"])
            self.assertEqual(route_b_after["project_id"], "proj_b")

    def test_proxy_resolves_links_written_by_client_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "client.json"
            repo = root / "repo"
            repo.mkdir()
            link_repo(config_path=config_path, repo_root=repo, project_id="proj_cli")

            proxy = HttpProxyMcpServer(
                config=ProxyConfig(
                    repo_root=repo,
                    control_url="http://control.invalid",
                    project_links_path=root / "project_links.sqlite",
                )
            )

            self.assertEqual(proxy._resolve_project_id(), "proj_cli")


if __name__ == "__main__":
    unittest.main()
