from __future__ import annotations

import inspect
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from merv.brain.artifacts.ports import (
    AssociationTargetResolver,
    EvidenceReader,
)
from merv.brain.artifacts.resources import ResourceService
from merv.brain.kernel.utils import NotFoundError, ValidationError, WorkflowError
from merv.brain.research_core.association_targets import AssociationTargets
from merv.brain.research_core.domain.resource_evidence import (
    preferred_associated_resource,
    resource_state_record,
)
from tests.support.brain import TestBrain


class EvidenceReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Evidence")[
            "id"
        ]
        self.experiment_id = self.call(
            "experiment.create",
            project_id=self.project_id,
            name="evidence-reader",
            intent="Characterize submitted evidence.",
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def associate(self, *, path: str, role: str, body: bytes) -> dict:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        result = self.call(
            "resource.register",
            project_id=self.project_id,
            path=path,
            target_type="experiment",
            target_id=self.experiment_id,
            role=role,
        )
        return result["association"]

    def test_resource_service_conforms_and_preserves_semantic_pinned_records(self) -> None:
        self.assertIsInstance(self.app.resources, EvidenceReader)
        first = self.associate(
            path="plan.md",
            role="plan",
            body=(
                b"## Summary\nPinned v1.\n\n"
                b"## Objective & hypothesis\nTest the pin.\n\n"
                b"## Evaluation\nCompare versions.\n"
            ),
        )
        pinned_version = first["current_version_id"]
        (self.repo / "plan.md").write_text("working tree v2\n")
        live = self.call(
            "resource.register", project_id=self.project_id, path="plan.md"
        )
        self.associate(path="metrics.json", role="result", body=b'{"score": 1}\n')

        resources = self.app.resources.resources_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        self.assertEqual(
            [(item.role, item.path) for item in resources],
            [("plan", "plan.md"), ("result", "metrics.json")],
        )
        plan = resources[0]
        self.assertEqual(plan.submitted_version_id, pinned_version)
        self.assertEqual(plan.current_version_id, live["current_version_id"])
        self.assertNotEqual(plan.submitted_version_id, plan.current_version_id)
        self.assertGreater(plan.association_order, 0)

        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET revision_context = ? WHERE id = ?",
                ("uncommitted research write", self.experiment_id),
            )
            nested = self.app.resources.resources_for_target(
                target_type="experiment", target_id=self.experiment_id
            )
            document = self.app.resources.submitted_document(
                version_id=pinned_version,
                path="plan.md",
                role="plan",
                what="experiment plan",
            )
        self.assertEqual(nested, resources)
        self.assertIn("Pinned v1", document.text)

    def test_resource_service_matches_evidence_protocol_call_signatures(self) -> None:
        def call_shape(callable_value):
            return [
                (parameter.name, parameter.kind, parameter.default)
                for parameter in inspect.signature(callable_value).parameters.values()
            ]

        for name in (
            "resources_for_target",
            "resources_for_targets",
            "submitted_document",
            "submitted_evidence",
        ):
            self.assertEqual(
                call_shape(getattr(ResourceService, name)),
                call_shape(getattr(EvidenceReader, name)),
                name,
            )

    def test_plural_resources_handle_empty_duplicates_and_singular_order(self) -> None:
        self.assertEqual(
            self.app.resources.resources_for_targets(
                target_type="experiment", target_ids=()
            ),
            {},
        )
        prior = self.associate(
            path="z-result.json", role="result", body=b'{"score": 1}\n'
        )
        self.associate(path="b-plan.md", role="plan", body=b"plan b\n")
        self.associate(path="a-plan.md", role="plan", body=b"plan a\n")
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE resource_associations SET attempt_index = 0 "
                "WHERE resource_id = ? AND target_id = ?",
                (prior["id"], self.experiment_id),
            )

        singular = self.app.resources.resources_for_target(
            target_type="experiment", target_id=self.experiment_id
        )
        plural = self.app.resources.resources_for_targets(
            target_type="experiment",
            target_ids=(self.experiment_id, "exp_missing", self.experiment_id),
        )

        self.assertEqual(list(plural), [self.experiment_id, "exp_missing"])
        self.assertEqual(plural[self.experiment_id], singular)
        self.assertEqual(plural["exp_missing"], ())
        self.assertEqual(
            [(item.attempt_index, item.role, item.path) for item in singular],
            [
                (0, "result", "z-result.json"),
                (1, "plan", "a-plan.md"),
                (1, "plan", "b-plan.md"),
            ],
        )

    def test_plural_resources_preserve_corrupt_cross_project_associations(self) -> None:
        resource = self.associate(
            path="cross-project.md", role="result", body=b"tenant A evidence\n"
        )
        other_project_id = self.call(
            "project", action="create", name="Other tenant project"
        )["id"]
        other_experiment_id = self.call(
            "experiment.create",
            project_id=other_project_id,
            name="other-tenant-experiment",
            intent="Remain isolated from the first project.",
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO resource_associations (
                  id, resource_id, version_id, target_type, target_id, role,
                  attempt_index, created_at, created_seq
                ) VALUES (?, ?, ?, 'experiment', ?, 'plan', 1, ?, ?)
                """,
                (
                    "assoc_corrupt_cross_project",
                    resource["id"],
                    resource["current_version_id"],
                    other_experiment_id,
                    "2026-07-22T00:00:00Z",
                    999,
                ),
            )

        singular = self.app.resources.resources_for_target(
            target_type="experiment", target_id=other_experiment_id
        )
        plural = self.app.resources.resources_for_targets(
            target_type="experiment", target_ids=(other_experiment_id,)
        )

        self.assertEqual(plural[other_experiment_id], singular)
        self.assertEqual(len(singular), 1)
        self.assertEqual(singular[0].project_id, self.project_id)
        self.assertNotEqual(singular[0].project_id, other_project_id)

    def test_plural_resources_chunk_401_exact_ids_into_two_sqlite_reads(self) -> None:
        resource = self.associate(
            path="bulk-target.md", role="result", body=b"shared evidence\n"
        )
        target_ids = tuple(f"exp_bulk_{index:03d}" for index in range(401))
        with self.app.store.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO resource_associations (
                  id, resource_id, version_id, target_type, target_id, role,
                  attempt_index, created_at, created_seq
                ) VALUES (?, ?, ?, 'experiment', ?, 'result', 1, ?, ?)
                """,
                [
                    (
                        f"assoc_bulk_{index:03d}",
                        resource["id"],
                        resource["current_version_id"],
                        target_id,
                        "2026-07-22T00:00:00Z",
                        index + 1000,
                    )
                    for index, target_id in enumerate(target_ids)
                ],
            )

        statements: list[str] = []
        untraced_connect = self.app.store.connect

        def traced_connect():
            conn = untraced_connect()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(self.app.store, "connect", side_effect=traced_connect):
            result = self.app.resources.resources_for_targets(
                target_type="experiment", target_ids=target_ids
            )

        evidence_selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "association_target_id" in statement
        ]
        self.assertEqual(len(evidence_selects), 2)
        self.assertEqual(
            [statement.count("exp_bulk_") for statement in evidence_selects],
            [400, 1],
        )
        self.assertEqual(tuple(result), target_ids)
        self.assertTrue(
            all(
                len(result[target_id]) == 1
                and result[target_id][0].resource_id == resource["id"]
                for target_id in target_ids
            )
        )

    def test_role_precedence_and_figure_membership_are_evidence_facts(self) -> None:
        plan = self.associate(
            path="plan.md",
            role="plan",
            body=(
                b"## Summary\nCanonical.\n\n"
                b"## Objective & hypothesis\nPrefer this role.\n\n"
                b"## Evaluation\nRole precedence.\n"
            ),
        )
        self.associate(path="report.md", role="report", body=b"newer report\n")
        selected = preferred_associated_resource(
            resources=[
                resource_state_record(item)
                for item in self.app.resources.resources_for_target(
                    target_type="experiment", target_id=self.experiment_id
                )
            ],
            attempt=1,
            roles=("plan", "report"),
        )
        self.assertIsNotNone(selected)
        document = self.app.resources.submitted_document(
            version_id=selected["association_version_id"],
            path=selected["path"],
            role=selected["association_role"],
            what="submitted document",
        )

        self.assertEqual(document.role, "plan")
        self.assertEqual(document.version_id, plan["current_version_id"])
        self.assertEqual(document.figure_links, ())

    def test_strict_submitted_text_errors_remain_actionable(self) -> None:
        plan = self.associate(path="plan.md", role="plan", body=b"valid text\n")
        version_id = plan["current_version_id"]
        sha = plan["current_version"]["content_sha256"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE resource_associations SET version_id = NULL "
                "WHERE target_type = 'experiment' AND target_id = ? AND role = 'plan'",
                (self.experiment_id,),
            )
        with self.assertRaises(WorkflowError) as ctx:
            self.app.resources.submitted_document(
                version_id=None,
                path="plan.md",
                role="plan",
                what="experiment plan",
            )
        self.assertEqual(
            str(ctx.exception),
            "experiment plan (plan.md) has no pinned version — re-register it "
            "(resource.register with role 'plan') to submit the current content of plan.md",
        )

        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE resource_associations SET version_id = ? "
                "WHERE target_type = 'experiment' AND target_id = ? AND role = 'plan'",
                (version_id, self.experiment_id),
            )
        self.app.blobs.delete(namespace=self.project_id, sha256=sha)
        with self.assertRaises(WorkflowError) as ctx:
            self.app.resources.submitted_document(
                version_id=version_id,
                path="plan.md",
                role="plan",
                what="experiment plan",
            )
        self.assertEqual(
            str(ctx.exception),
            "experiment plan (plan.md) has no submitted content — re-register it "
            "(resource.register with role 'plan') to submit the current content of plan.md",
        )

        graph = self.associate(path="graph.json", role="graph", body=b"\xff")
        with self.assertRaises(WorkflowError) as ctx:
            self.app.resources.submitted_document(
                version_id=graph["current_version_id"],
                path="graph.json",
                role="graph",
                what="logic graph",
            )
        self.assertEqual(str(ctx.exception), "logic graph (graph.json) is not valid UTF-8 text")

        without_blobs = ResourceService(
            store=self.app.store,
            association_targets=AssociationTargets(store=self.app.store),
            blobs=None,
        )
        with self.assertRaises(WorkflowError) as ctx:
            without_blobs.submitted_document(
                version_id=version_id,
                path="plan.md",
                role="plan",
                what="experiment plan",
            )
        self.assertEqual(
            str(ctx.exception),
            "experiment plan: no blob store is configured; gated artifacts cannot be linted",
        )

    def test_reviewer_hydration_filters_roles_and_degrades_per_entry(self) -> None:
        plan = self.associate(path="plan.md", role="plan", body=b"submitted plan\n")
        self.associate(path="result.json", role="result", body=b'{"score": 1}\n')

        evidence = self.app.resources.submitted_evidence(
            target_type="experiment",
            target_id=self.experiment_id,
            attempt_index=1,
            roles=("plan", "result"),
        )
        self.assertEqual([(item.role, item.path) for item in evidence], [
            ("plan", "plan.md"),
            ("result", "result.json"),
        ])
        with patch.object(
            self.app.blobs, "get", wraps=self.app.blobs.get
        ) as get_blob:
            artifacts = self.app.reviews._submitted_artifacts(
                target_type="experiment",
                target_id=self.experiment_id,
                attempt_index=1,
            )
        self.assertEqual(get_blob.call_count, 1)
        self.assertEqual(
            artifacts,
            [
                {
                    "role": "plan",
                    "path": "plan.md",
                    "version_id": plan["current_version_id"],
                    "content": "submitted plan\n",
                }
            ],
        )

        self.app.blobs.delete(
            namespace=self.project_id,
            sha256=plan["current_version"]["content_sha256"],
        )
        unavailable = self.app.reviews._submitted_artifacts(
            target_type="experiment", target_id=self.experiment_id, attempt_index=1
        )
        self.assertIsNone(unavailable[0]["content"])
        self.assertEqual(
            unavailable[0]["note"],
            "submitted content unavailable; ask the producer to re-associate",
        )

    def test_target_resolver_is_typed_and_safe_inside_artifact_transaction(self) -> None:
        resolver = AssociationTargets(store=self.app.store)
        self.assertIsInstance(resolver, AssociationTargetResolver)
        with self.app.store.transaction():
            target = resolver.resolve(
                target_type="experiment", target_id=self.experiment_id
            )
        self.assertEqual(target.project_id, self.project_id)
        self.assertEqual(target.attempt_index, 1)
        self.assertEqual(
            resolver.resolve(target_type="attempt", target_id="implicit").attempt_index,
            0,
        )
        with self.assertRaisesRegex(NotFoundError, "experiment not found"):
            resolver.resolve(target_type="experiment", target_id="exp_missing")
        with self.assertRaisesRegex(ValidationError, "unsupported target type"):
            resolver.resolve(target_type="other", target_id="x")


if __name__ == "__main__":
    unittest.main()
