from __future__ import annotations

import unittest

from merv.brain.research_core.graph_refs import GraphRefResolver


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, query, arguments):
        self.calls.append((query, arguments))
        ref, _project_id = arguments
        if ref == "claim_1":
            return _Cursor(
                {"id": ref, "statement": "Claim", "status": "active"}
            )
        if ref == "exp_1":
            return _Cursor({"id": ref, "intent": "Test", "status": "running"})
        return _Cursor(None)

    def close(self):
        return None


class _Cursor:
    def __init__(self, row) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _Store:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self):
        return self.connection


class GraphRefResolverTest(unittest.TestCase):
    def test_resolves_only_research_owned_prefixes(self) -> None:
        store = _Store()

        result = GraphRefResolver(store=store).resolve_index(
            project_id="proj_1",
            refs=(
                "claim_1",
                "results.json",
                "claim_missing",
                "res_missing",
                "exp_1",
            ),
        )

        self.assertEqual(
            result,
            {
                "claim_1": {
                    "type": "claim",
                    "resolved": True,
                    "claim_id": "claim_1",
                    "statement": "Claim",
                    "status": "active",
                },
                "claim_missing": {"type": "unknown", "resolved": False},
                "exp_1": {
                    "type": "experiment",
                    "resolved": True,
                    "experiment_id": "exp_1",
                    "intent": "Test",
                    "status": "running",
                },
            },
        )
        self.assertEqual(
            [arguments[0] for _query, arguments in store.connection.calls],
            ["claim_1", "claim_missing", "exp_1"],
        )


if __name__ == "__main__":
    unittest.main()
