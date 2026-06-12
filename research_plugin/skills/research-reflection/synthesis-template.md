# Synthesis artifacts

A reflection wave produces three kinds of files. Only the envelopes below are
enforced; everything else is your design.

## 1. Reflections — `syntheses/<syn_id>/reflections/<lens_id>.md`

One markdown file per lens, written and submitted by that lens's subagent
(role `reflection`). The filename must be `<lens_id>.md` — the
`submit_reflections` gate matches coverage by filename. The only enforced
rule beyond that: the file must exist and be non-empty.

A reflection that serves the synthesizer well usually states: what the lens
examined (with ids/paths), what it found, what surprised it, and what it
could not verify. The `dead_ends` lens should center its ledger table:

```markdown
| direction tested | setting | what happened | why it failed |
|---|---|---|---|
| longer warmup    | exp_a, attempt 2 | no effect beyond noise | LR floor dominated |
```

## 2. The project logic graph — e.g. `project/logic_graph.json` (role `graph`)

One **living** JSON file for the whole project, edited in place each wave.
Same envelope as experiment graphs (see
`skills/research-workflow/graph-template.md`): valid JSON `version: 1`,
unique node ids with non-empty labels, **at most 16 nodes**, acyclic edges,
under 16 KB.

It is the project's current logic state, not a log: nodes are lessons,
themes, dead-end patterns, open questions — whatever the story needs, in
your vocabulary. Prune freely; each published wave pins the version it
shipped, so history is never lost by editing. Use node `refs` to keep nodes
brief: `exp_…`, `claim_…`, `rev_…`, `syn_…` ids and repo-relative paths
(reflections, reports) all resolve to links in the UI.

```json
{
  "version": 1,
  "title": "Project logic — wave 3",
  "nodes": [
    { "id": "anchor", "kind": "established", "label": "LR schedule dominates batch effects",
      "refs": ["claim_…", "exp_…"] },
    { "id": "wall", "kind": "dead_end_pattern", "label": "Optimizer swaps: three failures, same cause",
      "refs": ["syntheses/syn_…/reflections/dead_ends.md"] },
    { "id": "next", "kind": "open_question", "label": "Does the anchor hold at 10x scale?",
      "refs": ["syn_…"] }
  ],
  "edges": [
    { "from": "anchor", "to": "next", "label": "raises" },
    { "from": "wall", "to": "next", "label": "constrains" }
  ]
}
```

## 3. Proposals — e.g. `project/proposals.md` (role `proposals`)

The next wave's experiment candidates. Envelope: exists, non-empty. The
shape below is guidance the reviewer will recognize, not a schema:

```markdown
# What's next — wave 3 proposals

## P1 · Scale check on the LR anchor   ← at least one non-incremental bet,
Hypothesis: the schedule win survives 10x params.     or say why none exists
builds_on: claim_…, exp_…
Moves claim: claim_… (supported → tested at scale)

## P2 · Why did warmup-free work? (post-win follow-up)
Hypothesis: the gain came from gradient clipping interaction, not the
schedule itself — test the property through a different mechanism.
builds_on: exp_…

## P3 · Optimizer swap, fourth attempt — differs from the ledger
The dead-end ledger shows three optimizer-swap failures, all traced to LR
floor coupling. This proposal differs: it re-tunes the floor jointly.
builds_on: syntheses/syn_…/reflections/dead_ends.md
```

Accepted proposals become `experiment.create` calls whose plans (or graphs)
ref the `syn_` id that motivated them — convention, not a gate.
