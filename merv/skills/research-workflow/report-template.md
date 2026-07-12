<!--
  Results report template.

  This file is the FACE of the EXECUTED experiment: it is what the user reads
  in the UI to understand what happened, and the artifact the experiment
  reviewer grades against the plan's pre-registered Evaluation section. Write
  it in the experiment folder (e.g. experiments/<name>/report.md), then
  register + associate it with role "report".

  REQUIRED spine — `experiment.transition(submit_results)` is blocked until
  each of these has real content (the lint strips these HTML comments, so a
  section left as just guidance counts as empty):
    - Summary
    - Results          (when the system pins a metrics exhibit: MUST reference
                        and interpret it — see Results below)
    - Deviations from plan
    - Conclusion

  HARD LIMITS, also lint-enforced at submit_results:
    - The report must stay under 16 KB. This is the executive layer: raw
      numbers, logs, and large tables live in linked result resources
      (results.json, metrics.csv), not here.
    - Every relative image link must resolve to a local file under 5 MB, or
      resource.register rejects the report. Save figures next to the report
      (e.g. figures/*.png), copy them off any sandbox first, then submit.

  RECOMMENDED — not lint-enforced, but the experiment reviewer judges whether
  they are sufficient:
    - Figures (2–3 PNGs: the curves that justify the conclusion)
    - A machine-readable results.json companion:
      [{"metric": ..., "task": ..., "seed": ..., "target": ..., "achieved": ...}]
-->

# <Experiment title — one line, matching the plan>

## Summary
<!-- 2–4 plain-language sentences: what was run (model, data, scale, seeds)
     and the headline outcome. Written for someone scanning the UI. -->

## Results
<!--
  When the system can build a metrics exhibit, it contains up to the newest 50
  MLflow runs in this attempt's window plus eligible pinned result JSON and is
  pinned at submit_results as this folder's metrics exhibit JSON. It records
  when the run cap is reached. Preview with `experiment.exhibit` BEFORE writing
  this section, then write the interpretation around it:

  If `experiment.exhibit` returns a pinned exhibit:
  - Reference it by name (required whenever it exists), e.g.
    "All runs: [metrics exhibit](<exhibit json>)" — spell out its filename.
  - Read out the decisive comparisons in prose or a small summary view,
    citing run names/ids from the exhibit — never numbers that aren't in it.
  - Address ALL runs the exhibit shows, not just the good ones: failed seeds
    and aborted runs need a sentence each.
  - Use the exact metrics named in the plan's Evaluation section.

  An unconfigured/no-run fallback can have quantitative result files without a
  pinned exhibit. In that case, interpret the registered result evidence and
  explain why no exhibit was available; do not invent an exhibit link.
-->

## Figures
<!-- RECOMMENDED. Relative image links to figures saved next to this report:
     ![validation accuracy vs steps](figures/val_accuracy.png)
     Max ~3 — the curves that justify the conclusion, not a gallery. -->

## Deviations from plan
<!-- What differed from the approved design (data, hyperparameters, scale,
     procedure) and why — or the single word "None". Undisclosed deviations
     discovered by the reviewer are grounds for rejection. -->

## Conclusion
<!-- Quote the plan's decision rule and success threshold, then apply them to
     the table above: met / not met / partially met, at what scope. Do not
     conclude beyond the pre-registered rule — narrow beats inflated. -->
