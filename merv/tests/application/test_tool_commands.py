from __future__ import annotations

import unittest
from unittest.mock import Mock, call

from merv.brain.application.tool_commands import ControlToolOperations
from merv.brain.kernel.utils import ValidationError


class ControlToolOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.projects = Mock()
        self.claims = Mock()
        self.research = Mock()
        self.resources = Mock()
        self.storage = Mock()
        self.operations = ControlToolOperations(
            project_create=self.projects.create,
            project_get=self.projects.get,
            claims_list=self.claims.list_claims,
            research=self.research,
            resource_resolve=self.resources.resolve,
            resources_list=self.resources.list_resources,
            storage_resolve=self.storage.resolve,
            storage_list=self.storage.list_objects,
            storage_actions={
                action: getattr(self.storage, action)
                for action in ("pin", "unpin", "renew", "delete")
            },
        )

    def test_experiment_list_preserves_the_slim_projection_and_order(self) -> None:
        full = [
            {"id": "exp_2", "private": "two"},
            {"id": "exp_1", "private": "one"},
        ]
        self.research.project_experiments.return_value = full
        self.research.present_experiment.side_effect = lambda state: {
            "id": state["id"]
        }

        result = self.operations.experiment_list(project_id="proj_1")

        self.assertEqual(result, {"experiments": [{"id": "exp_2"}, {"id": "exp_1"}]})
        self.research.project_experiments.assert_called_once_with(project_id="proj_1")
        self.assertEqual(
            self.research.present_experiment.call_args_list,
            [call(full[0]), call(full[1])],
        )

    def test_project_create_forwards_only_the_historical_arguments(self) -> None:
        self.projects.create.return_value = {"id": "proj_1"}

        result = self.operations.project(
            action="create",
            project_id="ignored",
            name="Project",
            summary="Summary",
            overwrite=True,
            tenant_id="tenant_1",
            user_id="user_1",
        )

        self.assertEqual(result, {"id": "proj_1"})
        self.projects.create.assert_called_once_with(
            name="Project",
            summary="Summary",
            tenant_id="tenant_1",
            user_id="user_1",
        )

    def test_project_overview_reuses_claim_and_slim_experiment_projections(self) -> None:
        self.projects.get.return_value = {
            "id": "proj_1",
            "name": "Project",
            "summary": "Summary",
            "extra": "hidden",
        }
        self.claims.list_claims.return_value = {"claims": [{"id": "claim_1"}]}
        self.research.project_experiments.return_value = [{"id": "exp_1"}]
        self.research.present_experiment.return_value = {"id": "exp_1", "status": "planned"}

        result = self.operations.project(action="overview", project_id="proj_1")

        self.assertEqual(
            result,
            {
                "project": {
                    "id": "proj_1",
                    "name": "Project",
                    "summary": "Summary",
                },
                "claims": [{"id": "claim_1"}],
                "experiments": [{"id": "exp_1", "status": "planned"}],
            },
        )
        self.projects.get.assert_called_once_with(project_id="proj_1")
        self.claims.list_claims.assert_called_once_with(project_id="proj_1")

    def test_proxy_owned_project_actions_keep_the_upgrade_error(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            self.operations.project(action="connect", project_id="proj_1")

        self.assertEqual(
            str(raised.exception),
            'project action="connect" is served by the local merv proxy, not the '
            "brain. Seeing this means your Merv client is older than the brain — "
            "update the plugin (git pull) and restart your MCP client.",
        )

    def test_resource_find_preserves_resolve_and_list_modes(self) -> None:
        self.resources.resolve.return_value = {"resource": {"id": "res_1"}}
        resolved = self.operations.resource_find(
            project_id="proj_1", resource_id="res_1", include_history=True
        )
        self.assertEqual(resolved, {"resource": {"id": "res_1"}})
        self.resources.resolve.assert_called_once_with(
            resource_id="res_1", include_history=True, project_id="proj_1"
        )
        self.resources.list_resources.return_value = {"resources": []}

        listed = self.operations.resource_find(
            project_id="proj_1",
            kind="report",
            experiment_id="exp_1",
            missing=False,
            compact=True,
            limit=4,
            offset=2,
        )

        self.assertEqual(listed, {"resources": []})
        self.resources.list_resources.assert_called_once_with(
            kind="report",
            experiment_id="exp_1",
            missing=False,
            compact=True,
            limit=4,
            offset=2,
            project_id="proj_1",
        )

    def test_storage_find_preserves_resolve_and_list_modes(self) -> None:
        self.storage.resolve.return_value = {"object": {"id": "so_1"}}
        resolved = self.operations.storage_find(
            project_id="proj_1",
            object_id="so_1",
            version=3,
            include_download=False,
        )
        self.assertEqual(resolved, {"object": {"id": "so_1"}})
        self.storage.resolve.assert_called_once_with(
            project_id="proj_1",
            object_id="so_1",
            name=None,
            version=3,
            include_download=False,
        )
        self.storage.list_objects.return_value = {"objects": []}

        listed = self.operations.storage_find(
            project_id="proj_1",
            kind="model",
            status="ready",
            include_expired=True,
            limit=5,
            offset=1,
            compact=True,
        )

        self.assertEqual(listed, {"objects": []})
        self.storage.list_objects.assert_called_once_with(
            project_id="proj_1",
            kind="model",
            status="ready",
            include_expired=True,
            limit=5,
            offset=1,
            compact=True,
        )

    def test_storage_object_routes_each_action_and_preserves_unknown_error(self) -> None:
        for action in ("pin", "unpin", "renew", "delete"):
            with self.subTest(action=action):
                operation = getattr(self.storage, action)
                operation.return_value = {"action": action}
                self.assertEqual(
                    self.operations.storage_object(
                        project_id="proj_1", object_id="so_1", action=action
                    ),
                    {"action": action},
                )
                operation.assert_called_once_with(
                    project_id="proj_1", object_id="so_1"
                )

        with self.assertRaisesRegex(
            ValidationError, "unknown storage object action: purge"
        ):
            self.operations.storage_object(
                project_id="proj_1", object_id="so_1", action="purge"
            )


if __name__ == "__main__":
    unittest.main()
