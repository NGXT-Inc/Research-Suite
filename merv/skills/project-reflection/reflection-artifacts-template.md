# Reflection artifacts

A reflection wave produces fan-out reflection docs plus three reviewed reflection
artifacts. Only the envelopes below are enforced; everything else is your
design.

## 1. Reflections — `syntheses/<syn_id>/reflections/<lens_id>.md`

One markdown file per lens, written and submitted by that lens's subagent
(role `reflection_lens_doc`). The filename must be `<lens_id>.md` — the
`submit_reflections` gate matches coverage by filename. The only enforced
rule beyond that: the file must exist and be non-empty.

A reflection that serves the synthesizer well usually states: what the lens
examined (with ids/paths), what it found, what surprised it, and what it
could not verify. The `avoid` lens should center its ledger table:

```markdown
| direction tested | setting | what happened | why it failed | what would have to change |
|---|---|---|---|---|
| longer warmup    | exp_a, attempt 2 | no effect beyond noise | LR floor dominated | retry only with a lower LR floor |
```

The ledger is cumulative across waves: re-verify still-binding rows from the
previous wave's `avoid` reflection and carry them forward, so the current
table stands alone.

## 2. The project logic graph — e.g. `project/logic_graph.json` (role `project_graph`)

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
      "refs": ["syntheses/syn_…/reflections/avoid.md"] },
    { "id": "next", "kind": "open_question", "label": "Does the anchor hold at 10x scale?",
      "refs": ["syn_…"] }
  ],
  "edges": [
    { "from": "anchor", "to": "next", "label": "raises" },
    { "from": "wall", "to": "next", "label": "constrains" }
  ]
}
```

## 3. Reflection document — e.g. `project/reflection.md` (role `reflection_doc`)

This markdown file is the orchestrator's concise scientific reading of the
five reflections. It is not a long report and not a paste-up of the five
subagent outputs. Keep it under 16 KB. Make it visual-friendly: use compact
tables, bullets, and one or two visual elements when they help a scientist
scan the project state quickly.

Required headings:

```markdown
# Reflection

## Summary
One short paragraph stating what the wave changes about the project state.

## Critical reading
Two to five paragraphs or bullets that reconcile the lenses against the
actual project records: what holds up, what remains uncertain, what was
ruled out, and where the lenses disagree.

## Decision / future directions
One short paragraph explaining why the change spec's next experiment wave is
the right course for the project.
```

Optional figures should be relative markdown image links to files next to the
reflection document. Use the available image-generation capability when a
generated visual would make the reflection clearer: for example, a compressed
project-state map, a claim/evidence heatmap, a dead-end ledger visual, or a
future-direction decision diagram. Save generated images under a local
`figures/` folder next to the reflection document, then link them like this:

```markdown
![compressed project graph](figures/project_graph.png)
```

Every relative image link must resolve to a local file under 5 MB before you
associate the reflection doc — a dangling or oversized link is rejected at
associate. Save the image file, register the reflection doc, and associate it
with role `reflection_doc`. Associating the markdown submits the linked image
bytes too; if you add or change a figure later, re-associate the reflection
doc. Do not
add decorative visuals; every image should carry project reasoning that the
text would otherwise make slow to inspect.

## 4. Change spec — e.g. `project/change_spec.json` (role `change_spec`)

This JSON file is the reviewed **belief-state update** for the project. The
server validates that it can be materialized, and
`reflection.transition(publish)` applies it only after the
`reflection_reviewer` has passed the wave.

The decision is always `create_experiments`: update/create claims, then
create the approved planned experiments as real project experiments. Include
1-3 experiments; when the wave has more than one, give each a `parallelism`
note explaining why it can run independently of the rest. Stopping the
project is not in the spec's vocabulary — winding down is the researcher's
call, made outside the workflow.

Use claim `key`s when a newly-created claim is referenced by a proposed
experiment in the same change spec.

```json
{
  "version": 1,
  "claim_changes": [
    {
      "op": "update",
      "claim_id": "claim_existing",
      "status": "supported",
      "confidence": "high",
      "rationale": "Reviewed experiments exp_a and exp_b agree within the registered decision rules."
    },
    {
      "op": "create",
      "key": "claim_scale_transfer",
      "statement": "The LR schedule effect transfers at 10x scale.",
      "scope": "Same model family and dataset family as the current project.",
      "confidence": "medium",
      "rationale": "The project graph now identifies scale transfer as the live uncertainty."
    }
  ],
  "decision": {
    "type": "create_experiments",
    "experiments": [
      {
        "key": "scale_transfer",
        "name": "scale-transfer",
        "intent": "Test whether the LR schedule effect transfers at 10x scale.",
        "tested_claim_refs": ["claim_scale_transfer"],
        "parallelism": "Independent scale axis; can run beside mechanism-probe because it only depends on the published reflection."
      },
      {
        "key": "mechanism_probe",
        "name": "mechanism-probe",
        "intent": "Test whether the gain comes from clipping interaction rather than the schedule itself.",
        "tested_claim_refs": ["claim_scale_transfer"],
        "parallelism": "Independent mechanism axis; no dependency on the scale-transfer result."
      }
    ]
  }
}
```
