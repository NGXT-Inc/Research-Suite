from __future__ import annotations

import unittest

from merv.brain.application.resource_content import HostedResourceContentQuery
from merv.brain.kernel.utils import NotFoundError, WorkflowError


class FakeArtifacts:
    def __init__(self, selected=None, error: Exception | None = None) -> None:
        self.selected = selected
        self.error = error
        self.resource = {
            "id": "res_1",
            "path": "reports/result.md",
            "project_id": "proj_1",
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def resolve_resource(self, **kwargs):
        self.calls.append(("resolve", kwargs))
        if isinstance(self.error, NotFoundError):
            raise self.error
        return self.resource

    def select_resource_text(self, **kwargs):
        self.calls.append(("select", kwargs))
        if self.error is not None:
            raise self.error
        return self.selected


class HostedResourceContentQueryTest(unittest.TestCase):
    def test_implicit_success_preserves_legacy_shape_without_available(self) -> None:
        artifacts = FakeArtifacts(selected=("hello", "ver_2"))

        result = HostedResourceContentQuery(artifacts=artifacts)(
            project_id="proj_1", resource_id="res_1"
        )

        self.assertEqual(
            result,
            {
                "resource": artifacts.resource,
                "path": "reports/result.md",
                "content": "hello",
                "text": "hello",
                "size_bytes": 5,
                "source": "submitted",
                "version_id": "ver_2",
            },
        )

    def test_explicit_success_marks_content_available(self) -> None:
        result = HostedResourceContentQuery(
            artifacts=FakeArtifacts(selected=("hé", "ver_1"))
        )(project_id="proj_1", resource_id="res_1", version_id="ver_1")

        self.assertEqual(result["size_bytes"], 3)
        self.assertIs(result["available"], True)
        self.assertEqual(result["version_id"], "ver_1")

    def test_empty_version_keeps_implicit_shape(self) -> None:
        result = HostedResourceContentQuery(
            artifacts=FakeArtifacts(selected=("hello", "ver_2"))
        )(project_id="proj_1", resource_id="res_1", version_id="")

        self.assertNotIn("available", result)

    def test_implicit_missing_bytes_uses_hosted_degraded_shape(self) -> None:
        result = HostedResourceContentQuery(artifacts=FakeArtifacts())(
            project_id="proj_1", resource_id="res_1"
        )

        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "content_unavailable_in_this_mode")
        self.assertIsNone(result["content"])
        self.assertNotIn("version_id", result)

    def test_explicit_missing_bytes_converts_workflow_error_only(self) -> None:
        artifacts = FakeArtifacts(error=WorkflowError("blob missing"))

        result = HostedResourceContentQuery(artifacts=artifacts)(
            project_id="proj_1", resource_id="res_1", version_id="ver_9"
        )

        self.assertEqual(result["reason"], "version_unavailable")
        self.assertEqual(result["detail"], "blob missing")
        self.assertEqual(result["version_id"], "ver_9")

    def test_resource_scope_errors_are_not_hidden(self) -> None:
        query = HostedResourceContentQuery(
            artifacts=FakeArtifacts(error=NotFoundError("wrong project"))
        )

        with self.assertRaisesRegex(NotFoundError, "wrong project"):
            query(project_id="proj_2", resource_id="res_1")


if __name__ == "__main__":
    unittest.main()
