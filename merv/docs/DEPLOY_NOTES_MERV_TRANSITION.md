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
