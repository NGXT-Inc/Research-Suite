from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from backend.tools.contracts import MCP_HIDDEN_TOOL_NAMES
from backend.utils import ValidationError


class ProjectToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def test_project_view_omits_legacy_hard_stop_fields(self) -> None:
        # The hard-stop mechanism is gone; views must not resurface its
        # legacy columns even on databases that still carry them.
        project = self.call("project", action="create", name="Alpha")
        self.assertNotIn("hard_stop_reflection_id", project)
        self.assertNotIn("hard_stop_rationale", project)
        self.assertNotIn("stopped_at", project)

        fetched = self.call("project.get", project_id=project["id"])
        self.assertNotIn("hard_stop_reflection_id", fetched)
        self.assertNotIn("hard_stop_rationale", fetched)
        self.assertNotIn("stopped_at", fetched)

    def test_legacy_stopped_project_reactivates_on_migration(self) -> None:
        project = self.call("project", action="create", name="Alpha")
        # Simulate a database stopped under the removed hard-stop contract,
        # before the reactivation migration existed.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET status = 'stopped' WHERE id = ?",
                (project["id"],),
            )
            conn.execute(
                "DELETE FROM schema_migrations WHERE name = 'reactivate_hard_stopped_projects'"
            )
        self.app.store._initialize()
        fetched = self.call("project.get", project_id=project["id"])
        self.assertEqual(fetched["status"], "active")

    def test_project_name_must_be_at_least_three_chars_on_create_and_update(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="create", name="ab")
        self.assertIn("at least 3", str(ctx.exception))

        project = self.call("project", action="create", name="Alpha")
        with self.assertRaises(ValidationError) as empty_ctx:
            self.call("project.update", project_id=project["id"], name=" ")
        self.assertIn("name is required", str(empty_ctx.exception))

        with self.assertRaises(ValidationError) as short_ctx:
            self.call("project.update", project_id=project["id"], name="xy")
        self.assertIn("at least 3", str(short_ctx.exception))

        updated = self.call("project.update", project_id=project["id"], name="Beta")
        self.assertEqual(updated["name"], "Beta")

    def test_hidden_project_is_stashed_from_list_but_retained(self) -> None:
        keep = self.call("project", action="create", name="Keep")
        stash = self.call("project", action="create", name="Stash")

        self.call("project.update", project_id=stash["id"], hidden=True)

        # project.list (the UI project picker) omits the hidden project...
        listed = {p["id"] for p in self.call("project.list")["projects"]}
        self.assertIn(keep["id"], listed)
        self.assertNotIn(stash["id"], listed)

        # ...but the row and direct-by-id access are fully retained.
        fetched = self.call("project.get", project_id=stash["id"])
        self.assertEqual(fetched["name"], "Stash")
        self.assertTrue(fetched["settings"]["hidden"])

        # Restoring returns it to the list (reversible).
        self.call("project.update", project_id=stash["id"], hidden=False)
        restored = {p["id"] for p in self.call("project.list")["projects"]}
        self.assertIn(stash["id"], restored)

    def test_action_create_requires_name(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="create")
        self.assertIn("name", str(ctx.exception))

    def test_action_create_rejects_short_name(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="create", name="ab")
        self.assertIn("3", str(ctx.exception))

    def test_action_connect_requires_id_or_name(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="connect")
        self.assertIn("project_id", str(ctx.exception))

    def test_action_current_rejects_extra_fields(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="current", name="Alpha")
        self.assertIn("current", str(ctx.exception))

    def test_action_overview_rejects_extra_fields(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="overview", name="Alpha")
        self.assertIn("overview", str(ctx.exception))

    def test_action_overview_reads_all_claims_and_experiments(self) -> None:
        # overview returns every claim including a non-active one (the
        # whole-project read). The proxy injects project_id as the scope; a
        # direct caller passes it — the tool schema keeps it visible.
        pid = self.app.current_project()["project"]["id"]
        claim = self.call("claim.create", project_id=pid, statement="Overview claim.")
        self.call("claim.update", project_id=pid, claim_id=claim["id"], status="abandoned")
        overview = self.call("project", action="overview", project_id=pid)
        self.assertEqual(overview["project"]["id"], pid)
        self.assertEqual(
            {c["id"]: c["status"] for c in overview["claims"]}[claim["id"]],
            "abandoned",
        )
        self.assertIn("experiments", overview)

    def test_brain_reports_current_is_proxy_served(self) -> None:
        # A valid action=current passes validation but the brain never serves
        # it — the local proxy does. Reaching the brain means a stale client.
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="current")
        self.assertIn("proxy", str(ctx.exception))

    def test_brain_reports_connect_is_proxy_served(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project", action="connect", project_id="proj_x")
        self.assertIn("proxy", str(ctx.exception))

    def test_current_and_connect_error_over_direct_http_mcp_call(self) -> None:
        # Same domain error over the wire: a direct HTTP /mcp/call (an old proxy
        # or a raw caller) gets the actionable "update your client" message.
        client = TestClient(self.app.fastapi_app)
        for action in ("current", "connect"):
            arguments: dict = {"action": action}
            if action == "connect":
                arguments["project_id"] = "proj_x"
            response = client.post(
                "/mcp/call", json={"name": "project", "arguments": arguments}
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("proxy", response.text)

    def test_action_create_forwards_over_direct_http_mcp_call(self) -> None:
        client = TestClient(self.app.fastapi_app)
        response = client.post(
            "/mcp/call",
            json={
                "name": "project",
                "arguments": {"action": "create", "name": "Http Made"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["name"], "Http Made")

    def test_project_list_is_hidden_from_agents(self) -> None:
        self.assertIn("project.list", MCP_HIDDEN_TOOL_NAMES)

    def test_update_without_hidden_leaves_hidden_unchanged(self) -> None:
        project = self.call("project", action="create", name="Alpha")
        self.call("project.update", project_id=project["id"], hidden=True)
        self.call("project.update", project_id=project["id"], summary="edited")
        fetched = self.call("project.get", project_id=project["id"])
        self.assertTrue(fetched["settings"]["hidden"])
        self.assertEqual(fetched["summary"], "edited")


if __name__ == "__main__":
    unittest.main()
