from __future__ import annotations

import json
import unittest

from backend.domain.graph_lint import (
    GRAPH_SCHEMA_VERSION,
    MAX_GRAPH_BYTES,
    MAX_GRAPH_NODES,
    graph_problems,
)


def _graph(nodes, edges=None, version=GRAPH_SCHEMA_VERSION) -> str:
    return json.dumps({"version": version, "nodes": nodes, "edges": edges or []})


class GraphLintTest(unittest.TestCase):
    def test_minimal_valid_graph_passes(self) -> None:
        text = _graph(
            [
                {"id": "obj", "kind": "objective", "label": "Reproduce the paper"},
                {"id": "out", "kind": "outcome", "label": "Matched within 0.3"},
            ],
            [{"from": "obj", "to": "out", "label": "confirmed by"}],
        )
        self.assertEqual(graph_problems(text), [])

    def test_agent_owns_vocabulary_and_extra_fields_are_ignored(self) -> None:
        # The envelope does not police node kinds, edge labels, statuses, or
        # unknown fields — the story's design belongs to the agent.
        text = _graph(
            [
                {"id": "a", "kind": "rabbit hole", "label": "Chased a red herring", "mood": "regret"},
                {"id": "b", "kind": "breakthrough", "label": "Tokenizer was the culprit", "refs": ["rev_1"]},
            ],
            [{"from": "a", "to": "b", "label": "led, eventually, to"}],
        )
        self.assertEqual(graph_problems(text), [])

    def test_invalid_json_is_one_plain_problem(self) -> None:
        problems = graph_problems("{not json")
        self.assertEqual(len(problems), 1)
        self.assertIn("not valid JSON", problems[0])

    def test_non_object_and_wrong_version(self) -> None:
        self.assertTrue(graph_problems("[1, 2]"))
        problems = graph_problems(_graph([{"id": "a", "label": "A"}], version=2))
        self.assertTrue(any("version" in p for p in problems))

    def test_nodes_must_be_a_non_empty_list(self) -> None:
        self.assertTrue(graph_problems(json.dumps({"version": 1, "nodes": []})))
        self.assertTrue(graph_problems(json.dumps({"version": 1})))

    def test_node_budget_states_problem_without_prescription(self) -> None:
        nodes = [{"id": f"n{i}", "label": f"step {i}"} for i in range(MAX_GRAPH_NODES + 1)]
        problems = graph_problems(_graph(nodes))
        self.assertEqual(len(problems), 1)
        self.assertIn(f"{MAX_GRAPH_NODES + 1} nodes", problems[0])
        self.assertIn("reduce the graph", problems[0])
        # The message points out the problem; how to retell the story is the
        # agent's call — no consolidation recipe.
        self.assertNotIn("collapse", problems[0].lower())
        self.assertNotIn("merge", problems[0].lower())

    def test_exactly_sixteen_nodes_is_allowed(self) -> None:
        nodes = [{"id": f"n{i}", "label": f"step {i}"} for i in range(MAX_GRAPH_NODES)]
        self.assertEqual(graph_problems(_graph(nodes)), [])

    def test_node_ids_and_labels_are_required_and_unique(self) -> None:
        problems = graph_problems(
            _graph(
                [
                    {"id": "a", "label": "A"},
                    {"id": "a", "label": "A again"},
                    {"id": "b"},
                    {"label": "no id"},
                    "not an object",
                ]
            )
        )
        self.assertTrue(any("duplicate node id" in p for p in problems))
        self.assertTrue(any("'b' needs a non-empty string 'label'" in p for p in problems))
        self.assertTrue(any("needs a non-empty string 'id'" in p for p in problems))
        self.assertTrue(any("must be an object" in p for p in problems))

    def test_edges_must_reference_existing_nodes_without_self_loops(self) -> None:
        problems = graph_problems(
            _graph(
                [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                [
                    {"from": "a", "to": "ghost"},
                    {"from": "a", "to": "a"},
                    {"from": "a", "to": "b"},
                ],
            )
        )
        self.assertTrue(any("existing node ids" in p for p in problems))
        self.assertTrue(any("self-loop" in p for p in problems))

    def test_cycles_are_rejected_and_named(self) -> None:
        problems = graph_problems(
            _graph(
                [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}],
                [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "c"},
                    {"from": "c", "to": "b"},
                ],
            )
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("cycle", problems[0])
        self.assertIn("b", problems[0])
        self.assertIn("c", problems[0])
        self.assertNotIn("a,", problems[0])

    def test_edges_are_optional(self) -> None:
        self.assertEqual(
            graph_problems(json.dumps({"version": 1, "nodes": [{"id": "a", "label": "A"}]})),
            [],
        )

    def test_size_ceiling(self) -> None:
        big_detail = "x" * (MAX_GRAPH_BYTES + 100)
        text = _graph([{"id": "a", "label": "A", "detail": big_detail}])
        problems = graph_problems(text)
        self.assertTrue(any("bytes" in p for p in problems))

    def test_all_problems_reported_in_one_pass(self) -> None:
        nodes = [{"id": f"n{i}", "label": f"step {i}"} for i in range(MAX_GRAPH_NODES + 1)]
        nodes.append({"id": "dup", "label": "x"})
        nodes.append({"id": "dup", "label": "x"})
        problems = graph_problems(
            _graph(nodes, [{"from": "dup", "to": "missing"}], version=3)
        )
        self.assertGreaterEqual(len(problems), 3)


if __name__ == "__main__":
    unittest.main()
