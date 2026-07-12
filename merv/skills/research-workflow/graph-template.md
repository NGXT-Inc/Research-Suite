# Logic graph (graph.json)

The logic graph is one JSON repo file (e.g. `experiments/<name>/graph.json`)
associated with the experiment under role `graph`. It is a **qualitative
story you write about the logical path of the experiment**: the critical
questions that needed answers, the hard decisions and the reasoning behind
them, the pivots (including those forced by reviews), what was ruled out, and
what was learned. The UI renders it as a DAG the user explores while the
experiment runs and after it ends — it is the reader's main window into *why*
the experiment went the way it did.

## A story of reasoning, not an event log

Write it the way you would walk a colleague through what actually mattered.
Events may appear — a crash, a rejection, a surprising number — but as
anchors for reasoning, never as the structure. The structure is logic:
question → decision → consequence → lesson.

What the graph is NOT:

- **Not a pipeline or provenance diagram.** If your nodes are components
  (code, environment, observability) and your edges read `produces`,
  `contains`, `records`, `implements`, you have drawn dataflow, not the
  story. Lineage belongs in resources and the report.
- **Not a metrics dump.** A number earns a node only when it changed a
  decision; raw metrics belong in result files a node may `ref`.
- **Not generated.** Do not build it with a script over your result files —
  choosing what mattered IS the authorship, and a generator cannot make that
  judgment. Write the JSON yourself.

## You design the graph

The graph is yours to author. Node `kind` names, edge labels, structure, and
what deserves a node are editorial calls — record what shaped the experiment,
not every step. If a development adds no valuable information to the story,
you may choose not to add it. The vocabulary in the example below is
illustrative, not required. A useful test for every node: does it help answer
"what did we have to figure out here, what did we choose, and why?"

## Envelope (the only server-enforced rules)

`experiment.transition(submit_results)` is blocked until the current attempt
has a role-`graph` resource whose SUBMITTED content (the bytes captured
when you associate it — re-associate after every edit you want counted)
passes these checks:

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
`refs` array takes plain strings — repo-relative paths of registered files
(`experiments/<name>/results.json`) or known record ids (`res_…`, `rev_…`,
`claim_…`, `exp_…`). The UI resolves them on read and renders them as links,
so the user and the reviewer can jump from a node to the file, review, or
claim behind it. Unresolvable refs are shown grayed out, never an error —
whether and what to reference is your call. Files you reference should be
registered and associated as resources so the links resolve.

## Keeping it current

Start the graph early (the objective node costs one minute) and re-register it
as the story develops — the user watches submitted versions, and a decision is
best recorded in the moment you make it, while the reasoning is still fresh; a
graph reconstructed at the end keeps the events but loses the *why*. After a
review rejection, consider whether the rejection and the rework belong in the
story. If the graph is at the 16-node budget and something important must be
added, reduce the graph first; how to retell the story within the budget is
your call.

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
    { "id": "accum", "kind": "fix", "label": "Gradient accumulation 4x8",
      "detail": "Chose accumulation over a smaller model: keeps the paper's effective batch, costs only wall-clock." },
    { "id": "gap", "kind": "problem", "label": "Val accuracy 3.1 pts low" },
    { "id": "tok", "kind": "insight", "label": "Paper tokenizes at max_len 128, ours 64" },
    { "id": "rerun", "kind": "pivot", "label": "Re-tokenize and rerun all seeds",
      "detail": "Rerunning everything was cheaper than reasoning about which checkpoints the truncation tainted." },
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
