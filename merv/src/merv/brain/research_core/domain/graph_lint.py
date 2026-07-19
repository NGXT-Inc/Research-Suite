"""Pure envelope lint for the agent-authored experiment logic graph.

graph.json is a qualitative story the agent writes about the experiment's
logical path — the hard decisions, the reasoning behind them, pivots, and
lessons, told as a small DAG the UI renders live. Not an event or pipeline
diagram, and authored by hand, never script-generated. The agent designs the
graph: its vocabulary (node ``kind``), its shape, and what deserves a node
are editorial calls that belong to the author and are judged by the
experiment reviewer, not by this lint. The server checks only the envelope:
it parses, it stays within the node budget, and it renders as a DAG. Same
philosophy as the plan/report lints — shape here, substance in review.
"""

from __future__ import annotations

import json


GRAPH_SCHEMA_VERSION = 1

# The node budget is structural, like the report's 16 KB ceiling: a 40-node
# graph is a log; a 16-node graph is a story. How to retell the story within
# the budget is the agent's call — the lint only states the overrun.
MAX_GRAPH_NODES = 16

# Bounds the render and context cost without per-field length rules.
MAX_GRAPH_BYTES = 16_000


def graph_problems(graph_text: str) -> list[str]:
    """Everything wrong with a logic graph's envelope, in one pass.

    Problems are stated plainly — what is wrong, not how to rewrite the story.
    """
    problems: list[str] = []
    size = len(graph_text.encode("utf-8"))
    if size > MAX_GRAPH_BYTES:
        problems.append(
            f"graph file is {size} bytes; the maximum is {MAX_GRAPH_BYTES} — reduce it"
        )
    try:
        data = json.loads(graph_text)
    except json.JSONDecodeError as exc:
        problems.append(f"graph is not valid JSON: {exc}")
        return problems
    if not isinstance(data, dict):
        problems.append("graph must be a JSON object with 'nodes' and optional 'edges'")
        return problems
    if data.get("version") != GRAPH_SCHEMA_VERSION:
        problems.append(f"graph 'version' must be {GRAPH_SCHEMA_VERSION}")

    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        problems.append("graph 'nodes' must be a non-empty list")
        return problems
    if len(nodes) > MAX_GRAPH_NODES:
        problems.append(
            f"graph has {len(nodes)} nodes; the maximum is {MAX_GRAPH_NODES} — reduce the graph"
        )
    known_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            problems.append(f"nodes[{index}] must be an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            problems.append(f"nodes[{index}] needs a non-empty string 'id'")
            continue
        if node_id in known_ids:
            problems.append(f"duplicate node id: {node_id}")
            continue
        known_ids.add(node_id)
        label = node.get("label")
        if not isinstance(label, str) or not label.strip():
            problems.append(f"node '{node_id}' needs a non-empty string 'label'")

    edges = data.get("edges") or []
    if not isinstance(edges, list):
        problems.append("graph 'edges' must be a list")
        return problems
    valid_edges: list[tuple[str, str]] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            problems.append(f"edges[{index}] must be an object")
            continue
        frm, to = edge.get("from"), edge.get("to")
        if frm not in known_ids or to not in known_ids:
            problems.append(
                f"edges[{index}] must reference existing node ids in 'from' and 'to'"
            )
            continue
        if frm == to:
            problems.append(f"edges[{index}] is a self-loop on '{frm}'")
            continue
        valid_edges.append((str(frm), str(to)))

    cycle = _cycle_problem(node_ids=known_ids, edges=valid_edges)
    if cycle:
        problems.append(cycle)
    return problems


def _cycle_problem(*, node_ids: set[str], edges: list[tuple[str, str]]) -> str | None:
    """Kahn's algorithm: if a topological order can't cover every node, the
    leftover nodes sit on a cycle — name them so the agent knows where."""
    out: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    for frm, to in edges:
        out[frm].append(to)
        indegree[to] += 1
    queue = [node_id for node_id in node_ids if indegree[node_id] == 0]
    visited: set[str] = set()
    while queue:
        node_id = queue.pop()
        visited.add(node_id)
        for nxt in out[node_id]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    leftover = sorted(node_ids - visited)
    if leftover:
        return (
            "graph contains a cycle (must be a DAG); nodes on the cycle: "
            + ", ".join(leftover)
        )
    return None
