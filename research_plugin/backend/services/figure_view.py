"""Derived experiment-figure projection (Phase 0 of the figure feature).

Builds a graph document — typed nodes + edges — for one experiment from state
the backend already owns: the attempt chain, resource associations (with
attempt indices), review verdicts, sandbox liveness, conclusion, and tested
claims. Nothing here is agent-authored; every node is derived and therefore
true by construction. A later phase merges an agent-authored overlay (arms,
decisions, metrics, lessons) into the same document shape.

Pure projection logic — no DB or backend calls. The HTTP layer gathers the
inputs (experiment state, review snapshots, open review requests, sandbox
view) and hands them in.

Node `status` values are normalized for UI coloring:
  pending | active | done | failed | superseded | abandoned
except `review` nodes, whose status is the verdict (pass | needs_changes |
fail | open) and `claim` nodes, whose status is the claim status.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ..sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES

FIGURE_SCHEMA_VERSION = 1

# Resource roles that feed an attempt vs. ones an attempt produces.
UPSTREAM_ROLES = {"plan", "input", "code", "config", "model"}

# Per-attempt, per-direction cap on individual resource nodes. Sandbox syncs
# can associate hundreds of files to one attempt; past the cap the remainder
# rolls up into a single `resource_group` node so the canvas stays readable.
RESOURCE_FANOUT_CAP = 6

# Which resources survive the cap, most-load-bearing first.
_ROLE_PRIORITY = {"plan": 0, "report": 1, "result": 2, "model": 3, "input": 4, "code": 5, "config": 6, "note": 7}

_ATTEMPT_STATUS = {
    "planned": "pending",
    "design_review": "pending",
    "ready_to_run": "pending",
    "running": "active",
    "experiment_review": "active",
    "complete": "done",
    "failed": "failed",
    "abandoned": "abandoned",
}

_REVIEW_LABELS = {
    "design_reviewer": "Design review",
    "experiment_reviewer": "Experiment review",
    "human": "Human review",
    "automated_check": "Automated check",
}


def _humanize(value: str) -> str:
    return value.replace("_", " ")


def _resource_label(resource: dict[str, Any]) -> str:
    title = (resource.get("title") or "").strip()
    if title:
        return title
    return PurePosixPath(str(resource.get("path") or resource.get("id") or "resource")).name


def build_experiment_figure(
    *,
    experiment: dict[str, Any],
    review_attempts: dict[str, int],
    open_review_requests: list[dict[str, Any]],
    sandbox: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project one experiment's state into a figure graph.

    `review_attempts` maps review id -> attempt_index (resolved from review
    snapshots by the caller; 0 means unknown). `sandbox` is a sandbox row view
    or None when the experiment never had one.
    """
    current_attempt = max(1, int(experiment.get("attempt_index") or 1))
    status = str(experiment.get("status") or "planned")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def add_edge(source: str, target: str, edge_type: str) -> None:
        edges.append(
            {
                "id": f"{source}->{target}:{edge_type}",
                "from": source,
                "to": target,
                "type": edge_type,
            }
        )

    def clamp_attempt(value: Any) -> int:
        try:
            attempt = int(value)
        except (TypeError, ValueError):
            attempt = 0
        if attempt < 1 or attempt > current_attempt:
            return current_attempt
        return attempt

    # ---- attempt spine ----
    for k in range(1, current_attempt + 1):
        is_current = k == current_attempt
        nodes.append(
            {
                "id": f"attempt:{k}",
                "type": "attempt",
                "label": f"Attempt {k}",
                "sublabel": _humanize(status) if is_current else "superseded",
                "status": _ATTEMPT_STATUS.get(status, "pending") if is_current else "superseded",
                "group": f"attempt:{k}",
                "ref": {"kind": "experiment", "id": experiment.get("id")},
            }
        )
        if k > 1:
            add_edge(f"attempt:{k - 1}", f"attempt:{k}", "revised_to")

    # ---- resources, one node per (resource, attempt) association ----
    # Bucket by (attempt, direction), keep the most load-bearing files under
    # the fan-out cap, and roll the rest into one expandable group node.
    buckets: dict[tuple[int, bool], list[dict[str, Any]]] = {}
    seen_assoc: set[tuple[str, int]] = set()
    for res in experiment.get("resources", []):
        attempt = clamp_attempt(res.get("association_attempt_index"))
        key = (str(res.get("id")), attempt)
        if key in seen_assoc:
            continue
        seen_assoc.add(key)
        role = str(res.get("association_role") or "other")
        buckets.setdefault((attempt, role in UPSTREAM_ROLES), []).append(res)

    for (attempt, upstream), bucket in sorted(buckets.items()):
        bucket.sort(
            key=lambda r: (
                _ROLE_PRIORITY.get(str(r.get("association_role") or "other"), 9),
                str(r.get("path") or ""),
            )
        )
        shown, overflow = bucket[:RESOURCE_FANOUT_CAP], bucket[RESOURCE_FANOUT_CAP:]
        for res in shown:
            role = str(res.get("association_role") or "other")
            node_id = f"res:{res.get('id')}:a{attempt}"
            nodes.append(
                {
                    "id": node_id,
                    "type": "resource",
                    "label": _resource_label(res),
                    "sublabel": role,
                    "status": "none",
                    "group": f"attempt:{attempt}",
                    "ref": {
                        "kind": "resource",
                        "id": res.get("id"),
                        "version_id": res.get("association_version_id"),
                    },
                    "meta": {"role": role, "path": res.get("path"), "kind": res.get("kind")},
                }
            )
            if upstream:
                add_edge(node_id, f"attempt:{attempt}", "feeds")
            else:
                add_edge(f"attempt:{attempt}", node_id, "produced")
        if overflow:
            roles = sorted({str(r.get("association_role") or "other") for r in overflow})
            node_id = f"resgroup:a{attempt}:{'up' if upstream else 'down'}"
            nodes.append(
                {
                    "id": node_id,
                    "type": "resource_group",
                    "label": f"{len(overflow)} more files",
                    "sublabel": " · ".join(roles),
                    "status": "none",
                    "group": f"attempt:{attempt}",
                    "ref": {"kind": "resource_group", "id": None},
                    "meta": {
                        "count": len(overflow),
                        "roles": roles,
                        "resource_ids": [str(r.get("id")) for r in overflow],
                    },
                }
            )
            if upstream:
                add_edge(node_id, f"attempt:{attempt}", "feeds")
            else:
                add_edge(f"attempt:{attempt}", node_id, "produced")

    # ---- submitted reviews, attached to their attempt ----
    for review in experiment.get("reviews", []):
        review_id = str(review.get("id"))
        attempt = clamp_attempt(review_attempts.get(review_id))
        verdict = str(review.get("verdict") or "")
        node_id = f"review:{review_id}"
        nodes.append(
            {
                "id": node_id,
                "type": "review",
                "label": _REVIEW_LABELS.get(str(review.get("role")), "Review"),
                "sublabel": _humanize(verdict),
                "status": verdict or "open",
                "group": f"attempt:{attempt}",
                "ref": {"kind": "review", "id": review_id},
                "meta": {
                    "role": review.get("role"),
                    "synopsis": review.get("synopsis") or "",
                    "notes": review.get("notes") or "",
                },
            }
        )
        add_edge(f"attempt:{attempt}", node_id, "reviewed_by")
        # A needs_changes verdict is what spawns the next attempt: draw the loop.
        if verdict == "needs_changes" and attempt < current_attempt:
            add_edge(node_id, f"attempt:{attempt + 1}", "revised_to")

    # ---- open review gates (requested/started, no verdict yet) ----
    for request in open_review_requests:
        node_id = f"review_request:{request.get('id')}"
        nodes.append(
            {
                "id": node_id,
                "type": "review",
                "label": _REVIEW_LABELS.get(str(request.get("role")), "Review"),
                "sublabel": "awaiting verdict",
                "status": "open",
                "group": f"attempt:{current_attempt}",
                "ref": {"kind": "review_request", "id": request.get("id")},
            }
        )
        add_edge(f"attempt:{current_attempt}", node_id, "reviewed_by")

    # ---- sandbox / execution ----
    if sandbox and str(sandbox.get("status") or "none") != "none":
        sandbox_status = str(sandbox.get("status"))
        nodes.append(
            {
                "id": "sandbox",
                "type": "sandbox",
                "label": "Sandbox",
                "sublabel": str(sandbox.get("gpu") or sandbox.get("instance_type") or sandbox_status),
                "status": "active" if sandbox_status in ACTIVE_SANDBOX_STATUSES else "done",
                "group": f"attempt:{current_attempt}",
                "ref": {"kind": "sandbox", "id": experiment.get("id")},
                "meta": {"sandbox_status": sandbox_status},
            }
        )
        add_edge(f"attempt:{current_attempt}", "sandbox", "ran_on")

    # ---- conclusion + tested claims ----
    conclusion = str(experiment.get("conclusion") or "").strip()
    claim_source = f"attempt:{current_attempt}"
    if conclusion:
        nodes.append(
            {
                "id": "conclusion",
                "type": "conclusion",
                "label": "Conclusion",
                "sublabel": conclusion,
                "status": "done",
                "group": f"attempt:{current_attempt}",
                "ref": {"kind": "experiment", "id": experiment.get("id")},
            }
        )
        add_edge(claim_source, "conclusion", "concludes")
        claim_source = "conclusion"
    for claim in experiment.get("tested_claims", []):
        node_id = f"claim:{claim.get('id')}"
        nodes.append(
            {
                "id": node_id,
                "type": "claim",
                "label": str(claim.get("statement") or claim.get("id")),
                "sublabel": _humanize(str(claim.get("status") or "")),
                "status": str(claim.get("status") or "active"),
                "ref": {"kind": "claim", "id": claim.get("id")},
            }
        )
        add_edge(claim_source, node_id, "tests")

    return {
        "schema_version": FIGURE_SCHEMA_VERSION,
        "source": "derived",
        "experiment_id": experiment.get("id"),
        "intent": experiment.get("intent") or "",
        "status": status,
        "attempt_index": current_attempt,
        "groups": [
            {"id": f"attempt:{k}", "label": f"Attempt {k}"} for k in range(1, current_attempt + 1)
        ],
        "nodes": nodes,
        "edges": edges,
    }
