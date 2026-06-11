# Logic graph (graph.json)

The logic graph is one JSON repo file (e.g. `experiments/<name>/graph.json`)
associated with the experiment under role `graph`. It is the **story of how
the experiment actually went**, told by you: the notable decisions, the
problems you ran into, the pivots and iterations (including those forced by
reviews), and what was learned. The UI renders it as a DAG the user can
explore while the experiment runs and after it ends.

## You design the graph

The graph is yours to author. Node `kind` names, edge labels, structure, and
what deserves a node are editorial calls — record what shaped the experiment,
not every step. If a development adds no valuable information to the story,
you may choose not to add it. The vocabulary in the example below is
illustrative, not required.

## Envelope (the only server-enforced rules)

`experiment.transition(submit_results)` is blocked until the current attempt
has a role-`graph` resource whose live file passes these checks:

- valid JSON object with `"version": 1`
- `nodes`: non-empty list; every node has a unique string `id` and a
  non-empty string `label`
- **at most 16 nodes**
- `edges` (optional): every `from`/`to` references an existing node id; no
  self-loops; the graph is acyclic (it must render as a DAG)
- file under 16 KB

Everything else — `kind`, `detail`, `refs`, edge `label`, any extra fields —
is yours and is ignored by the lint. Substance is judged by the experiment
reviewer, not the linter.

## Refs: brief nodes, linked detail

Prefer brief nodes that point at evidence over long `detail` prose. A node's
`refs` array takes plain strings — repo-relative paths of synced files
(`experiments/<name>/results.json`) or known record ids (`res_…`, `rev_…`,
`claim_…`, `exp_…`). The UI resolves them on read and renders them as links,
so the user and the reviewer can jump from a node to the file, review, or
claim behind it. Unresolvable refs are shown grayed out, never an error —
whether and what to reference is your call. Files you reference should be
synced (and usually registered as resources) so the links resolve.

## Keeping it current

Start the graph early (the objective node costs one minute) and sync it as
the story develops — the user watches it live. After a review rejection,
consider whether the rejection and the rework belong in the story. If the
graph is at the 16-node budget and something important must be added, reduce
the graph first; how to retell the story within the budget is your call.

## Shape

```json
{
  "version": 1,
  "title": "optional — your name for this story",
  "nodes": [
    {
      "id": "unique-string",
      "label": "short, required",
      "kind": "optional, free-form — your vocabulary",
      "detail": "optional prose",
      "refs": ["optional anchors: review ids, resource paths, run ids"]
    }
  ],
  "edges": [
    { "from": "node-id", "to": "node-id", "label": "optional, free-form" }
  ]
}
```

## Example (illustrative vocabulary, not a schema)

```json
{
  "version": 1,
  "title": "Reproducing DistilBERT SST-2",
  "nodes": [
    { "id": "obj", "kind": "objective", "label": "Reproduce 91.3% on SST-2" },
    { "id": "plan2", "kind": "pivot", "label": "Plan v2 after design review",
      "detail": "Reviewer required an explicit decision rule.", "refs": ["rev_..."] },
    { "id": "oom", "kind": "problem", "label": "OOM on A10 at batch 32" },
    { "id": "accum", "kind": "fix", "label": "Gradient accumulation 4x8" },
    { "id": "gap", "kind": "problem", "label": "Val accuracy 3.1 pts low" },
    { "id": "tok", "kind": "insight", "label": "Paper tokenizes at max_len 128, ours 64" },
    { "id": "rerun", "kind": "pivot", "label": "Re-tokenize and rerun all seeds" },
    { "id": "out", "kind": "outcome", "label": "91.0% ± 0.2 — claim supported" }
  ],
  "edges": [
    { "from": "obj", "to": "plan2" },
    { "from": "plan2", "to": "oom" },
    { "from": "oom", "to": "accum" },
    { "from": "accum", "to": "gap" },
    { "from": "gap", "to": "tok", "label": "traced to" },
    { "from": "tok", "to": "rerun" },
    { "from": "rerun", "to": "out" }
  ]
}
```
