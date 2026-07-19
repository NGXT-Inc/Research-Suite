# Deploy notes: Merv clean transition

One-time notes for the release that finishes the RapidReview → Merv rename
(on-box vocabulary, MLflow namespace, synthesis → reflection unification).
Delete this file once the release is deployed and the one-version shims are
removed.

## On-box rename (sandboxes)

Sandboxes are ephemeral VMs bootstrapped from current code, so the on-box
vocabulary (`merv_run`, `MERV_EXPERIMENT_DIR`, `/opt/merv/`, `mervmgmt`,
`99-merv.conf`, `.merv_sessions`, the `MERV ` metrics prefix) ships in ONE
sweep — emitters and brain-side parsers change together, with no dual-read
parsing.

**Release/drain all active sandboxes before upgrading the brain.** A brain at
this release cannot read transcripts, metrics, or run receipts from a box
bootstrapped by the previous release (old mgmt user `rpmgmt`, old `RPM `
metrics lines, old `===RP_RUN` listing blocks), and vice versa.

One-version compat shims installed by the new bootstrap, to be removed next
release:

- `rp_run` on PATH as a symlink to `merv_run`.
- `RP_EXPERIMENT_DIR` exported as a deprecated twin of `MERV_EXPERIMENT_DIR`.

## MLflow namespace rp/ -> merv/

New experiments are created under `merv/<project>/<experiment>`
(MLFLOW_NAMESPACE_PREFIX in src/merv/brain/mlflow/tracking.py). Run at deploy, on
prod and against local dev MLflow:

```
python3 merv/scripts/migrate_mlflow_namespace.py --dry-run   # inspect plan
python3 merv/scripts/migrate_mlflow_namespace.py             # rename in place
```

Idempotent and safe to re-run; name collisions are skipped with a report.
MLflow lookups are name-based, so an un-migrated server keeps working — but
existing experiments stay reachable under their old `rp/...` names only
(metrics ledger, exhibits, and namespace listings will not see them as
`merv/...`) until the script runs.

## Synthesis -> reflection unification (migration 19)

The reflection wave drops its internal synthesis vocabulary; only the
consolidation phase keeps the name `synthesizing`. Migration 19 runs
automatically on first boot after the pull (control-up.sh path) and rewrites
persisted state: the two wave-relation tables (+ columns), the
`synthesis_review` status, events history (types, target_type, payload
vocabulary), review target types and snapshot ids, and association target
types. UI and brain deploy in lockstep from main — the UI reads only the
post-migration shapes (`reflections`/`open_reflection` keys,
`reflection_review` status, `reflection.*` event types, the `/reflection`
mobile route).

## Deploy order (prod)

1. Release/drain all active sandboxes (on-box rename, above).
2. MERV key-flip in the Azure compose override where still pending (tracked
   in the ops dir notes).
3. `pg_dump` backup, fast-forward pull, `control-up.sh` — applies migration
   19 on boot.
4. `python3 merv/scripts/migrate_mlflow_namespace.py` against prod MLflow
   (after a `--dry-run` look).
5. Vercel picks up the UI from main; verify the Home reflection panel, the
   mobile `/reflection` screen, and review history on a reflection wave.
