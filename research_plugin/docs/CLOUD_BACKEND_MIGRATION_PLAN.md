# Cloud Backend Migration Plan — dual-mode control/data plane split

**Status:** plan (approved direction, not yet implemented) · **Authored:** 2026-06-12
**Supersedes/extends:** [CONTROL_DATA_PLANE_SPLIT.md](CONTROL_DATA_PLANE_SPLIT.md) (its module split table and
sync-session/lease contract are adopted and elaborated here).
**Provenance:** design conversation of 2026-06-12; the fixed decisions in §2 were taken there.

## 1. Goal and non-goals

Make the backend deployable to a cloud VM as a **multi-tenant control plane**, while the **same codebase**
keeps supporting fully-local hosting. Three process roles, one repo:

```text
RESEARCH_PLUGIN_MODE = local   → today's topology: one process binds BOTH planes in-process
                                 (SQLite, .research_plugin/blobs/, provider creds from local .env)
RESEARCH_PLUGIN_MODE = control → cloud control plane: Postgres, S3 blobs, auth/tenancy,
                                 provider creds, provisioner + reaper, lints on pinned bytes
RESEARCH_PLUGIN_MODE = daemon  → slim local data-plane daemon: rsync, SSH keys/conn files,
                                 file observation + artifact submission, repo_root↔project map,
                                 long-poll task loop (cloud never dials in)
```

`local` is the default, forever, and must be green at the end of **every** phase below. The MCP proxy stays
a thin stateless stdio process and gains dual upstreams in split mode.

Non-goals (unchanged from the MVP exclusions): bring-your-own provider keys (later), SSH CA (later
evolution), relay byte transport (fallback only), broad RBAC.

## 2. Fixed decisions (constraints, not options)

1. **Dual-mode, one codebase.** Mode is selected in composition only; services are mode-blind.
2. **Three roles** as above; the cloud never initiates connections to user machines — every
   "cloud signals daemon" flow is a daemon-initiated long-poll **task channel** that degenerates to a
   synchronous in-process queue in local mode.
3. **Platform-owned provider credentials** (Lambda/Modal keys live cloud-side; never resolved on user
   machines in split mode; local-mode `.env` discovery preserved).
4. **Per-sandbox management SSH keypair**, minted control-side and injected at bootstrap alongside the
   user key. Transcripts, metrics sampling, reaping, and the parachute ride the management key; the user
   key never leaves the user machine and is used only by the data plane (rsync, sbx dispatcher, tunnels).
5. **Expiry parachute:** at reap, signal the daemon to final-pull within a deadline; if unreachable, run a
   pre-installed tar script on the VM via the management channel and upload to a single-use presigned PUT;
   record `{object_key, sha256, size, expires_at}` on the row; daemon restores on reconnect; TTL backstop.
6. **Artifact submission model:** gated-role files (plan, report, graph, proposals, reflection) upload
   bytes at `resource.associate`; associations pin a `version_id`; lints run on pinned bytes; semantics
   change from "fix the live file clears the gate" to "fix and resubmit". Figures upload via a presigned
   tier driven by the report lint. Result files stay metadata-only. No background file sync, no watchers.
7. **Blob store:** content-addressed by sha256, per-tenant namespace, TTL support; one service shared by
   submissions, figures, metrics snapshots, and parachute objects. Local impl: a `.research_plugin/blobs/`
   directory.
8. **Leases:** cloud-held, exclusive per experiment, TTL+takeover; the only multi-client coordination
   authority. Sync sessions carry a `direction_policy` closing the rsync `--delete` footgun.

Two corrections discovered during inventory, now part of the plan's premises:

- The "<16 KB by existing lints" premise of decision 6 is **false today** for plan/reflection/proposals
  (no byte caps in code — only report 10 KB at `artifacts.py:41` and graph 16 KB at `graph_lint.py:24-27`).
  Caps for all five gated roles are added *before* any inline-upload contract exists (Phase 1).
- There are **34** MCP tools in `TOOL_CONTRACTS`, and the routing table and partition tests are derived
  from that count, not hand-maintained lists.

## 3. Target architecture (contracts the phases are derived from)

### 3.1 Plane interfaces

- **`ControlPlane`** — record halves of all 34 tools plus seam verbs: `resource_record_observation`
  (metadata half of register_file), `resource_submit_artifact` (bytes→blob→version→pin→lint),
  `figure_presign`, `sandbox_report_initial_push`, `sandbox_report_sync` (lease-checked),
  `sandbox_metrics_submit`, `sync_session_get`, `lease_acquire/renew/release`,
  `daemon_poll_tasks`/`task_ack`, `blob_put_inline`/`blob_presign_put`/`blob_stat`.
  Two clients, one behavioral contract suite (the `test_sandbox_backend_contract.py` pattern):
  `InProcessControlPlaneClient` (local) and `HttpControlPlaneClient` (bearer token, split).
- **`DataPlaneWorker`** — every local-IO duty: `observe_file`/`read_artifact_bytes` (bytes read *before*
  any record transaction opens), `resolve_report_figures`, `ensure_workspace` (the `experiment.create`
  mkdir, today at `experiments.py:111-115` — in split mode there is no cloud→daemon "create folder"
  signal, so workspace creation becomes **lazy on the first data-routed touch** of the experiment
  (`resource.register_file` / `sandbox.request` / first push); `experiment.create`'s folder guidance
  becomes advisory), `ensure_keypair`/`write_conn_file`/`remove_conn_file` (`sandbox_conn.py`),
  `push_initial`/`sync`/`final_pull(session, deadline)` (wrapping `SshRsyncSyncer`),
  `ensure_local_dashboards` (split-mode trigger: the daemon's auto-sync loop, which already touches every
  running sandbox), `capture_metrics_fallback`, `restore_parachute`, plus the background `auto_sync_loop`
  and `task_loop`. Interim duty (Phases 3–4 only): `read_transcript`/`sample_metrics` stay worker duties
  with the worker-held user key until Phase 5's management-key switch makes them control-feasible.
- **`BlobStore`** — `put / get / stat / presign_put(single_use, max_size, expected_sha256) / sweep_expired`,
  keyed `tenant/sha256`. Impls: `LocalDirBlobStore` (sidecar TTL metadata; "presign" = single-use loopback
  PUT token) and `S3BlobStore` (native presign; lifecycle TTL). `resource_versions.content_sha256` is the
  join key.
- **`StateStore`** — loses `repo_root` (today `store.py:270-273`); ordered migration ledger
  (`schema_migrations`) shared by a SQLite dialect (local) and a Postgres dialect (cloud). Postgres deltas:
  identity column for `events` (replaces AUTOINCREMENT), explicit ordering column for `resource_versions`
  (replaces `ORDER BY rowid`), `SELECT … FOR UPDATE` where `BEGIN IMMEDIATE` was load-bearing
  (attempt bumps, `current_version_id`), no default-project bootstrap (local-mode-only, `store.py:311-325`).
- **Sync session / lease / task channel** — per CONTROL_DATA_PLANE_SPLIT.md, with two additions:
  `transfer_contract_version` pinning the shared excludes/caps (one constants module feeds both rsync and
  the parachute tar), and task types `initial_push | final_pull | conn_refresh | parachute_restore |
  teardown`.

### 3.2 Identity, time, and machine-local data

- **Project identity decouples from `repo_root`.** The cloud mints `project_id` and never accepts a
  filesystem path. The `directory_projects` registry (`project_router.py:262-270`) is reclassified as
  daemon-local state (`repo_root ↔ project_id` mapping). The proxy resolves identity via the daemon and
  sends explicit `project_id` on cloud calls; `context.repo_root` never crosses the machine boundary.
- **Entity ids** stay prefix+uuid strings tenant-wide; only `events.id` (AUTOINCREMENT) needs re-keying on
  import (nothing FK-references it).
- **Clocks:** all deadlines (sandbox `expires_at`, capability expiry, lease TTLs, parachute deadlines) are
  **cloud-authoritative**; the daemon treats them as opaque and never compares them against its own clock
  for enforcement.
- **Machine-local values leave cloud-bound rows**: `sandboxes.key_path`, `sandboxes.local_sync_dir`,
  loopback dashboard URLs, and absolute paths in event payloads move to a daemon-owned store
  (`~/.research_plugin/daemon.sqlite`); cloud rows keep provider-portable facts only.
- **Telemetry is per-plane and machine-local by construction.** `ActivityLogger` (writes
  `activity.jsonl` on every tool call and HTTP request, `activity.py:77,90-91`, `http_api.py:690-706`)
  and `ToolCallStore` (`tool_calls.sqlite`, its own AUTOINCREMENT SQLite schema, `tool_calls.py:234,239`)
  are both constructed from `store.repo_root` today (`app.py:85,88-90`). Both become config-injected
  sinks: the daemon keeps the local files; the control plane gets its own sinks (DB-backed audit /
  structured stdout). Neither file is ever synced across the seam.

### 3.3 MCP tool routing (end state)

`ToolContract` gains `plane: "control" | "data" | "aggregate"` — the machine-checkable source of truth for
proxy routing and the partition test (`control ∪ data ∪ aggregate == TOOL_CONTRACTS`, pairwise disjoint).

| Route | Tools |
|---|---|
| → local daemon (`data`) | `resource.register_file`, `resource.associate`, `sandbox.request`, `sandbox.sync` |
| → both (`aggregate`) | `sandbox.health` (daemon self-check + cloud reachability + auth status); `sandbox.get` (cloud row facts + daemon enrichment — see below) |
| → cloud (`control`) | everything else: `workflow.status_and_next`, `project.*`, `claim.*`, `experiment.*`, `synthesis.*`, `resource.list/resolve/delete`, `review.*`, `sandbox.options/list/release/terminal` |

`sandbox.get` cannot be pure-control: today it lazily spawns dashboard tunnels (`sandboxes.py:270-289`,
`subprocess.Popen` at `sandbox_dashboards.py:251`) and renders the agent-facing `ssh.command` /
`local_dir` from conn files + `repo_root` (`sandbox_views.py:105-116`). The agent view decomposes:
provider-portable row facts come from the cloud; `command`, `local_dir`, loopback dashboards, and conn
state are daemon-side enrichment merged by the proxy/daemon. The `review.*` row is contingent on open
decision E (result re-observation at `review.start` would need an `observe_resources` task type or an
aggregate route).

`data`-routed tools act as the cloud's client for their record half (associate reads bytes locally, then
calls `resource_submit_artifact`). Proxy behavior in split mode: **fail loud** on missing config (no silent
`127.0.0.1:8787` fallback, `proxy.py:38,196-198`); distinct error codes returned as tool results, not
protocol errors (`local_daemon_not_running`, `cloud_unreachable`, `auth_expired`); cloud-down must not
block data tools and vice versa; long-running verbs (`sandbox.request`) return a handle and the agent
polls `sandbox.get`.

### 3.4 Config matrix

| Variable | local | daemon | control (cloud) |
|---|---|---|---|
| `RESEARCH_PLUGIN_MODE` | `local` (default) | `daemon` | `control` |
| `RESEARCH_PLUGIN_DB_URL` | — (SQLite path default) | — (daemon bookkeeping sqlite) | required (Postgres) |
| `RESEARCH_PLUGIN_BLOB_DIR` / `_BLOB_BUCKET` | blob dir default | — | bucket required |
| `RESEARCH_PLUGIN_CONTROL_URL` | — | required, fail-fast | self |
| `RESEARCH_PLUGIN_CONTROL_TOKEN_FILE` | — | `~/.research_plugin/credentials.json` (0600) | — |
| `RESEARCH_PLUGIN_DAEMON_URL` / marker | proxy → local process | proxy → daemon | — |
| provider creds (`MODAL_*`, `LAMBDA_*`) + `.env` discovery | today's behavior | **discovery disabled** | secret store only |
| reaper / auto-rsync | both on, in-process | rsync on, reaper off | reaper on, rsync off |
| `RESEARCH_PLUGIN_CLIENT_ID` | implicit | stable per-daemon id | — |

Mode validation is fail-fast: a daemon without a control URL, or a control plane without a DB URL, refuses
to start.

## 4. Phases

Sequencing logic: the **semantic change lands first** (Phases 1–2, pure local, can soak), then seams are
carved **in-process** (3–5) so every cross-plane interaction exists and is tested before any network hop,
then the record layer becomes cloud-grade (6–7), and only then does a network appear (8) and a cloud deploy
(9). Each phase is independently shippable.

---

### Phase 0 — Groundwork: mode flag, plane annotations, static catalog, ledger intro

- `contracts.py`: add `plane` to `ToolContract`; derive the three route sets; restate
  `test_tool_contracts.py` set-equality as the disjoint-union partition over all 34 tools.
- Serve `GET /mcp/tools` statically from `TOOL_CONTRACTS`; delete `tool_template_app()` and its
  `<registry_parent>/_tool_schema` filesystem side effect (`project_router.py:252-257`). It has a second
  consumer: `default_api()` backs the `/api/debug/tool-calls` endpoints (`http_api.py:650-654,741-752`) —
  re-point those in the same diff (resolve a real app via the router, or return an explicit
  "no project instantiated" response) so the deletion leaves no dangling consumer.
- `backend/config.py` (new): central mode/config resolution, fail-fast validation; only `local` valid yet.
- Migration ledger: `schema_migrations` table; freeze the introspective path (`_ensure_columns`,
  `_drop_columns`, autoindex rebuild, `store.py:399-503`) as the SQLite legacy-convergence step; move the
  destructive `DROP TABLE IF EXISTS jobs` (`store.py:235`) out of the every-boot SCHEMA constant.
- New `tests/structure/test_plane_layout.py` (AST import-lint pattern from `test_service_layout.py`),
  advisory at first; launcher hygiene (`bin/research-plugin-mcp` vestigial `RESEARCH_PLUGIN_STORE` export).

**Local mode:** byte-identical behavior. **Exit:** plane annotations complete; tool listing side-effect
free; ledger exists.

### Phase 1 — Blob store + byte capture at associate (additive, dual-write)

- `BlobStore` protocol + `LocalDirBlobStore` + `FakeBlobStore` (tests/fakes.py) + contract-pattern suite.
- `GATED_ROLES = {plan, report, graph, proposals, reflection}` next to `RESOURCE_ROLES`.
- `resources.associate` (`resources.py:196-237`): for gated roles, read bytes, enforce per-role size caps
  (16 KB; report 10 KB), `blob.put`, and pin the association to the minted `version_id`. **Live-file gate
  semantics unchanged this phase** — storage is additive, so this ships with near-zero risk and the caps
  land before any inline contract exists.
- `role=result` and other roles: metadata-only, unchanged.

**Local mode:** fully working; observable changes are the `blobs/` dir and the announced size cap.
**Exit:** every new gated association is blob-backed and version-pinned; all existing live-file tests pass.

### Phase 2 — Semantics flip: lints on pinned bytes, sweep deleted, prose + skills shipped together

The one intentional user-visible change of the whole migration. Soak in local mode before Phase 3.

- All six validators read pinned bytes via a `pinned_bytes(target, role, attempt)` helper:
  `experiments.py` plan/report/graph validators (`:497-614`) and `syntheses.py` roster/graph/prose
  validators (`:429-476`); guidance pre-lints in `workflow.py:299-319` likewise, with a lint cache keyed
  `(version_id, lint_version)` (inputs are immutable now). **No disk fallback** — in the enforcement path
  a missing blob raises; in the guidance path (`status_and_next`) it yields a `resubmit_required` gate
  with "re-associate this artifact" prose, never an exception. Grep gate: zero `store.repo_root` use in
  the validator/lint code paths (`_read_live_file` and the three experiment validators are deleted;
  `artifacts.py`/`graph_lint.py` stay pure-text) — the whole-module gate for `experiments.py` lands in
  Phase 3 with the mkdir move.
- **One-time blob backfill at upgrade** (the Phase 1 soak leaves gated associations whose pinned versions
  predate byte capture, or were re-pinned by the still-live sweep outside `associate`): for each
  gated-role pinned version with no blob, hash the working-tree file and `blob.put` it when it matches the
  version's `content_sha256` (the same rule as the Phase 8 import tool); the rest surface as
  `resubmit_required` rather than wedging.
- Figures: `artifacts.report_problems` (`:105-144`) split into pure link extraction + a
  `figure_exists` callback against figure blobs; associate(report) resolves links and uploads figures
  (local mode: same-process; split mode: the same list drives presigned PUTs). New `report_figures` table.
- **Delete the read-repair sweep** `refresh_target_resources` (`resources.py:388-441`) and all call sites
  in `workflow.py`; remove the `resource_refresh` block from `status_and_next`.
- Snapshot ids become version_id-backed; the `version_token` fallback survives only for metadata-only
  roles. One-time effect: in-flight review requests may need re-requesting (release-noted).
- `review.start` hydrates pinned gated-role bytes so reviewers review the snapshot, not the working tree.
- UI/HTTP endpoints re-pointed: gated-role `/content`, both graph endpoints, and report-figure `/file?rel=`
  serve pinned blob bytes; result-role content stays a live-disk read explicitly tagged local-only.
  `published_graph_version_id` now renders from immutable bytes — the dangling-pin problem closes.
  The graph endpoints' ref-index also stops probing the working tree (`_repo_file_exists`,
  `http_api.py:576,595-601`): node refs resolve against resource records / blob existence, and bare
  path-refs are marked unresolvable in control mode.
- **Agent-facing surface ships in the same diff (non-negotiable pairing).** The inventory found ~1,800
  lines across 9 skill/template files, 3 agent files, OpenCode wrappers, and 4 docs that hardcode live-file
  semantics — worst: `skills/research-workflow/SKILL.md:243-244` ("the lint reads the live file, so no
  re-registration is needed") and `:333-335` (read-repair reliance), plus the plan/report/graph templates
  and the figures-on-disk mandate. All flip to "submit bytes at associate; fix and re-associate".
  **Distribution caveat:** skills ship as install-time snapshots in four of five clients — the plugin
  `version` must be bumped and the release notes must say "reinstall", or installed agents keep following
  inverted instructions.

**Local mode:** fully working. **Exit:** zero gate/lint/guidance code paths read the working tree; inverted
tests (`tests/workflow/test_artifact_submission.py`) assert the new semantics; `test_local_shipping.py`
green unmodified (it already drives gates purely through register+associate).

### Phase 3 — In-process plane seam: composition split, repo_root removal, row de-localization

- Extract `ControlPlaneCore` + `LocalDataPlane` per §3.1; `app.py` becomes a thin binder; construction
  moves to `backend/composition/{local_mode,cloud_mode,daemon_mode}.py` (the latter two stubs).
- `StateStore` drops `repo_root`; `LocalWorkspace` (data plane) owns paths; `experiment.create`'s mkdir,
  resource file IO, conn files, tunnels, and rsync all route through `DataPlaneWorker` (workspace
  creation becomes lazy per §3.1). Whole-module grep gate lands here: zero `store.repo_root` in
  `experiments.py`, `syntheses.py`, `workflow.py`, `resources.py` (record half).
- **Telemetry construction moves off `store.repo_root`** (`app.py:85,88-90`): `ActivityLogger` and
  `ToolCallStore` are injected via `backend/config.py` per §3.2 — daemon/local keep the files, the control
  composition gets DB/stdout sinks (control-mode tool-call home finalized in Phase 6). The plane-layout
  lint explicitly covers `state/activity.py` and `state/tool_calls.py`.
- `SandboxService` splits along its existing decomposition: registry + provisioner + reaper + views →
  control; conn/tunnels/rsync/sessions → data. `push_initial` stops being an injected callable
  (`sandbox_provisioner.py:234-243`); `registry.on_terminal` routes through `DataPlaneWorker`
  (`remove_conn_file` + tunnel teardown) instead of a facade-injected callable — it becomes a `teardown`
  *task* only in Phase 4, where the task channel is born.
- **Agent-view decomposition** per §3.3: `sandbox.get`'s response splits into cloud row facts + daemon
  enrichment (`command`, `local_dir`, dashboards, conn state; today `sandboxes.py:270-289`,
  `sandbox_views.py:105-116`). Interim note: `read_transcript`/`sample_metrics` stay worker duties with
  the worker-held user key until Phase 5's management-key switch — the Phase 3 "IO-free control plane"
  exit criterion is scoped to exclude these two named interim paths.
- De-localize cloud-bound rows: `key_path`, `local_sync_dir`, loopback dashboard URLs move to
  daemon-owned state; event payloads stop carrying absolute paths (`sandboxes.py:583-592`).
- `tests/surface/test_control_plane_contract.py`: the dual-wiring behavioral scenario suite, in-process
  wiring only for now; includes the sanctioned test-only transition helper replacing raw-SQL status
  mutations so the suite can later run over the wire. Plane-layout lints become hard for control modules
  (no `ssh_rsync`/`subprocess`/`sandbox_conn`/repo paths).

**Local mode:** identical behavior; worker is an in-process object. **Exit:** control-plane imports are
provably IO-free; no machine-local values in cloud-bound rows; contract suite green in-process.

### Phase 4 — Sync sessions, leases, task channel (decision 8), routed in-process

- `LeaseService` + `SyncSession` issuance (with `direction_policy` and `transfer_contract_version`);
  `sandbox_report_sync` rejects stale leases. New `sync_leases` table.
- Task channel protocol + `InProcessTaskChannel` (synchronous — preserves today's reaper/push ordering
  exactly). Provision job restructured to explicit row states: `provisioning → awaiting_initial_push →
  running`, with cancellation/orphan cleanup covering the new state (daemon-offline mid-provision must
  never leave a billing VM with no files).
- Auto-sync poller re-pointed from direct DB reads (`sandbox_daemons.py:174`) to a `ControlPlaneView`
  ("my running sandboxes + leases") — the exact call that becomes an HTTP poll in Phase 8. Reaper's final
  pull becomes `worker.final_pull(session, deadline)` (parachute branch stubbed).
- `sandbox.get`/`list` expose lease state. (`start_sync`/`stop_sync` tool surface: deferred, open decision.)

**Local mode:** single implicit lease holder, asserted no-op; rsync mechanics byte-identical.
**Exit:** every byte movement flows through session + lease + task contracts in one process; two simulated
clients cannot double-sync one experiment.

### Phase 5 — Management keypair, cloud-owned bootstrap, metrics record, parachute (decisions 4 + 5)

- `MgmtKeyStore`: per-sandbox management ed25519 keypair minted control-side at provision. `SandboxRequest`
  carries both public keys; Modal `BOOT_SCRIPT` (`modal/sandbox_backend.py:79-82`) and Lambda
  `build_user_data` (`lambda_labs/sandbox_backend.py:684-693`) authorize both; Lambda adds an sshd `Match`
  exemption from the global ForceCommand for the management principal.
- Read-path switch: `read_transcript` and `sample_metrics` authenticate with the management key (Lambda)
  / control-plane exec (Modal); the user key becomes data-plane-only. Sequencing constraint honored:
  key injection precedes the read-path move.
- Shared `transfer_spec` constants module (rsync excludes + 100 MB/5 GB caps from `ssh_rsync.py:109-141,
  219-235`) consumed by rsync and the parachute tar; `/opt/rp/parachute.sh` pre-installed by both
  bootstraps (tar `$RP_EXPERIMENT_DIR` per spec, `curl -T` to a presigned URL argument).
- Reaper flow: final-pull task with deadline → on unreachable/timeout, presign single-use PUT + run
  parachute via exec/SSH → record `{object_key, sha256, size, expires_at}` → terminate → events
  `sandbox.parachuted` / `sandbox.parachute_failed` (loud failure surface). Restore: daemon task on
  reconnect, unpacked through the normal sync path; TTL backstop sweep. `sandbox.release` reuses the same
  final-pull-with-deadline machinery.
- **Metrics become a control-plane record:** snapshot JSON persisted as a record/blob (captured cloud-side
  where reachable; daemon pulled-`mlflow.db` fallback uploads via `sandbox_metrics_submit`), so reviews and
  UI see metrics without the user machine online. Resolves "daemon offline at reap = no metrics" — see
  open decision on parachute scope vs the sessions dir.
- Stated invariant (from inventory): `$RP_SANDBOX_DATA_DIR/.rp_runs/` env dumps (`bootstrap_tools.py:88`,
  containing HF_TOKEN) are excluded from both sync and parachute scope — correct today, now pinned by test.
- `FakeSandboxBackend` grows bootstrap/exec/upload capture hooks **here**, before the parachute tests need
  them (`BACKEND_METHODS` updated across Modal/Lambda/Fake in the same change).

**Local mode:** both keys generated on one machine, separation real and tested; parachute branch dormant
(daemon by definition reachable) but fully covered by fake-backend tests.
**Exit:** no control-plane code path touches the user private key; kill-the-daemon-then-reap provably
preserves the experiment dir; metrics survive VM death.

### Phase 6 — Store generalization: ledger completion, tenancy columns, Postgres dialect

- Tenancy columns: `tenant_id` on `projects` (+ ownership) and denormalized for scoping; local mode uses
  fixed `tenant_id='local'`; default-project bootstrap gated to local mode.
- Dialect work per §3.1 (`events` identity, `resource_versions` ordering column, `FOR UPDATE`,
  no PRAGMA/rowid/AUTOINCREMENT outside the SQLite dialect module). `version_token` demoted to a local
  observation field never compared cross-machine; cloud identity is `content_sha256` only.
- Control-mode tool-call audit home finalized: a tenant-scoped table in the ledger (replacing the
  daemon-side `tool_calls.sqlite`'s private AUTOINCREMENT schema, `tool_calls.py:239`) — or an explicit
  "VM-local file, admin-only" decision recorded here.
- CI gains a Postgres job running the record/workflow/review/synthesis suites against both dialects;
  ledger-drift and legacy-DB convergence tests extend `test_store_migrations.py`.

**Local mode:** SQLite untouched for users; existing DBs converge via the frozen legacy path.
**Exit:** same service code passes the full record-layer suite on SQLite and Postgres.

### Phase 7 — AuthN/authz, tenancy enforcement, identity mapping, quotas schema

- Principal middleware: `Authorization: Bearer` → `{tenant_id, client_id}`; local mode = auth off on
  loopback (today's behavior, bind enforced); control mode = mandatory on every route including `/mcp/*`.
  v1 token issuance: per-tenant tokens provisioned out of band; device-flow OAuth is an open decision to
  close before public multi-tenancy.
- Tenancy enforcement through `require_project_id` (`store.py:505-511`); cross-tenant denial tests on every
  verb family. `project.current` redefined per (tenant, repo-link) — the daemon resolves `repo_root →
  project_id` locally (`project_links`, successor of `directory_projects`); the cloud never sees paths;
  the router's mkdir side effect (`project_router.py:321-326`) becomes daemon-only.
- Capability hardening: review capabilities stored **hashed** (plaintext UNIQUE today, `store.py:109-123`),
  constant-time compare; review/producer sessions bound to authenticated principals (closes the
  attestation-based independence gap, `reviews.py:49,113-127`); reviewer `read_scope` server-enforced as
  "exactly the pinned snapshot's blob versions".
- **Cost-governance schema** (platform-owned keys make this a hard blocker, same class as auth):
  `tenant_quotas` (max concurrent sandboxes, max `time_limit`, instance-price ceiling, GPU-hour/USD budget,
  blob bytes) + `usage_counters`; sandbox rows record `price_usd_per_hour` at provision (fetched today at
  `lambda_labs/sandbox_backend.py:542-552` and discarded) and per-generation history stops being
  upsert-overwritten. Enforcement gates at the two choke points: `sandbox.request` admission and
  `provisioner.ensure_job`. The reaper's env-var off-switch is ignored in control mode.
- Surface hygiene: CORS tightened with `Authorization` allowed (today `*`, `http_api.py:683-688`);
  `/api/debug/*` + `/health` path leaks local-mode/admin-gated; `ToolCallStore` gains SENSITIVE_KEYS-style
  redaction.

**Local mode:** auth off, behavior identical; quotas inert under the single `local` tenant.
**Exit:** every entry point has a principal or is explicitly loopback-local; no plaintext capabilities at
rest; the cloud edge never receives a filesystem path; quota schema ready for enforcement.

### Phase 8 — Split transports live: entrypoints, dual-upstream proxy, packaging, import tool

- **Entrypoints:** `control` mode app (record services, lifecycle, blob, leases, task queue, control-tool
  `/mcp/*`, record `/api/*`) and `daemon` mode app (`LocalDataPlane` + `HttpControlPlaneClient` + task
  long-poll loop + auto-sync loop + local byte endpoints + `daemon.json` markers + `project_links`).
  The remote `DataPlaneWorker` is the task loop; the remote `ControlPlaneView` is an HTTP poll.
- **`S3BlobStore` lands here, not Phase 9** (it is already specified behind the same contract tests):
  the control entrypoint cannot run on `LocalDirBlobStore`, whose "presign" is a single-use *loopback*
  PUT token — unreachable from a sandbox VM, which would make the Phase 8 parachute smoke impossible.
- **Cloud reaper crash recovery lands here, not Phase 9**: the control entrypoint's startup scans tenant
  sandbox rows and resumes reaper/reconcile jobs (mirror of `_resume_active_sandbox_projects`,
  `project_router.py:279-312`). The split beta runs real billing VMs with the daemon-side reaper off, so
  a control restart with no recovery would leave Lambda VMs unreaped — exactly what risk 6 forbids.
  "Control restart mid-run re-acquires reaping" joins the split-smoke exit criteria. The provider-wide
  orphan-VM sweep stays in Phase 9.
- **Provision handshake live:** daemon keypair → cloud provision (both keys) → `awaiting_initial_push` →
  daemon push + confirm → `running`; conn files re-rendered on `conn_refresh`.
- **Proxy dual upstream:** route on the contract `plane` field; merged `tools/list` with uniform
  `project_id` stripping; bearer attach for cloud calls; per-tool timeouts (request→handle+poll); error
  taxonomy per §3.3. `.mcp.json` gains `RESEARCH_PLUGIN_CONTROL_URL`; **all five client manifest families**
  (`.mcp.json`, `.mcp.codex.json`, `mcp.json`, `gemini-extension.json`, `clients/opencode/*`) updated +
  plugin versions bumped (snapshot distribution).
- **Packaging profiles** (gap finding: one monolithic wheel, modal a hard dep, zero deploy artifacts):
  optional-dependency extras or split requirements — proxy stays stdlib-only (verified); daemon profile
  drops provider SDKs (modal is lazily imported at exactly one site, `modal/sandbox_backend.py:698`);
  control profile adds Postgres driver, object-store SDK, auth deps. New `bin/research-plugin-daemon`;
  `bin/research-plugin-http` stays the local-mode launcher.
- **Local→cloud onboarding** (gap finding): an explicit export/import tool — per-repo `state.sqlite` →
  tenant-scoped cloud DB; entity ids carry over, `events` re-keyed (order-preserving; nothing FKs it);
  gated-artifact blob backfill only where the working-tree file still matches the pinned `content_sha256`
  (everything else imports metadata-only, to be re-associated under the new model); precondition: no open
  review requests and no running sandboxes at flip; the local store gets a one-way tombstone so the two
  modes cannot silently diverge afterward.
- Daemon-as-credential-holder hardening: the daemon's loopback HTTP surface gets a local auth secret or
  unix socket before split-mode beta (it holds the cloud token and private keys).

**Local mode:** still the default; `mode=local` wiring is byte-for-byte the Phase 7 process; dual-upstream
paths inert without `RESEARCH_PLUGIN_CONTROL_URL`.
**Exit (split-mode beta):** the contract suite passes over both wirings with identical results; a real
split smoke (control on a VM, daemon on a laptop) completes the full loop — create → plan submit → design
review → sandbox request/push → sync under lease → results/report/figures → review → release with final
pull; killing the daemon mid-run exercises the parachute end-to-end.

### Phase 9 — Cloud productionization + GA

- **Deploy artifacts:** Dockerfile (uvicorn behind TLS/LB), managed Postgres, secret store for provider
  keys (all user-machine `.env` discovery disabled in control mode; `HF_TOKEN` handling per open
  decision; Lambda's plaintext `user_data` token embed replaced). S3 blob store and reaper crash recovery
  arrived in Phase 8; this phase productionizes them (lifecycle rules, alerting).
- **Quotas/rate limits enforced** (schema from Phase 7) + spend kill-switch per tenant and global; orphan
  VM sweep (provider-list vs rows), blob TTL GC, lease expiry sweep, stale `awaiting_initial_push` reaping.
- **Versioning/compat:** `/api/meta` returns `{server_version, min_daemon_version, min_proxy_version}`;
  clients send their version; below-floor rejected actionably; additive-only contract changes within a
  major.
- **Observability:** structured logs with request/tenant ids; per-tenant cloud audit (redacted) split from
  daemon-local `activity.jsonl` (never synced); RED metrics + per-tenant counters; alerts on reaper lag,
  provision failures, lease churn, parachute failures.
- **UI:** hosting decision executed (cloud-served SPA + auth; CORS per origin); viewers consume pinned-blob
  endpoints (done in Phase 2 server-side); result-role content gets an explicit degraded state or
  daemon-proxied read; sync/release buttons show task-queue + "daemon unreachable" states; **poll
  amplification addressed** — the UI is pure 3 s polling and `SandboxTerminal` would become a
  management-key SSH read per viewer per 3 s: add a control-side transcript cursor cache (and lint cache
  already in Phase 2); SSE/push is backlog.
- Chaos tests (daemon dies mid-provision / mid-sync), load tests, runbooks (abuse, tenant suspension, key
  and token rotation), docs overhaul (`ARCHITECTURE.md`, `MCP_SERVER_CONTRACT.md`,
  `CONTROL_DATA_PLANE_SPLIT.md` marked superseded).

**Local mode:** permanently supported tier-1 mode, exercised by CI on every commit (dual-mode parity suite:
one scenario corpus, both wirings, results compared).
**Exit (GA):** a second tenant cannot observe the first (records, blobs, events, sandboxes); deploy-restart
orphans nothing; quotas enforced; on-call-able.

## 5. Risk register (consolidated)

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| 1 | Semantics flip strands agents following stale skill snapshots | 2 | Skills/templates/prose in the same diff; plugin version bump + reinstall note; inverted tests |
| 2 | Disk-fallback in validators would fake the flip | 2 | No fallback — missing blob raises; grep/lint gate on `store.repo_root` |
| 3 | Snapshot-id change invalidates in-flight reviews | 2 | One-time re-request, release-noted; format otherwise unchanged |
| 4 | Size-cap premise false for 3 of 5 gated roles | 1 | Caps land with byte capture, before any inline contract |
| 5 | Report↔figure two-phase wedge | 2/8 | Idempotent re-associate; lint names missing figure blobs |
| 6 | Reaper liveness gap during transition (Lambda bills forever) | 4–9 | Reaper never ownerless: in-process until cloud reaper + restart-resume proven; both never disabled |
| 7 | Machine-local row values leak cloud-side | 3 | Column-level plane assignment before any row replicates |
| 8 | Daemon offline mid-provision → billing VM, no files | 4/8 | `awaiting_initial_push` + timeout → orphan cleanup; chaos-tested |
| 9 | Parachute silently loses results, or loses metrics (sessions dir excluded) | 5 | Loud `parachute_failed` surface; metrics record; open decision A |
| 10 | Proxy fails toward wrong default / wrong-plane blame | 8 | Hard-fail config in split mode; per-upstream error codes as tool results |
| 11 | Daemon = credential holder behind unauthenticated loopback | 8 | Local auth secret or unix socket before beta |
| 12 | SQLite/Postgres drift | 0/6 | Single ledger, dual-dialect CI, introspective path frozen as legacy-only |
| 13 | No cost governance with platform keys = unbounded spend (N experiments × $28/hr SKUs; re-request resets expiry) | 7/9 | Quota schema with auth (7), enforcement + kill-switch before multi-tenant exposure (9); price recorded per generation |
| 14 | Lint/poll cost scales with tenants; terminal poll = SSH per viewer per 3 s | 2/9 | Lint cache on immutable bytes; transcript cursor cache; SSE backlog |
| 15 | Structure-test counters mask regressions when bulk-updated | 3+ | Counters updated only in the phase diff that moves the code |
| 16 | HF_TOKEN plaintext in Lambda `user_data`, and env dumps on VM disk | 5/9 | Never log `user_data`; post-boot secret write; `.rp_runs` exclusion pinned by test; per-tenant secret store (open decision B) |

## 6. Open decisions

- **A — Parachute scope vs decision 5 letter:** the stated scope (experiment dir, rsync excludes) excludes
  the sessions dir where `mlflow.db` lives. Recommend including the sessions dir (bounded) in the tar;
  needs sign-off since it widens the stated scope. The Phase 5 metrics record reduces but does not remove
  the gap (no final snapshot at reap).
- **B — HF_TOKEN ownership in split mode:** per-tenant encrypted cloud secret (recommended) vs
  daemon-supplied per request.
- **C — Auth bootstrap:** static per-tenant tokens v1 (recommended) → device-flow OAuth before public
  multi-tenancy. Issuer (self-hosted vs external IdP) open.
- **D — User-key evolution:** per-experiment keypairs now (decision 4 letter); SSH CA with short-TTL certs
  as the production upgrade — decide whether bootstrap ships cert-ready sshd config from Phase 5.
- **E — Result-file integrity at review time:** result files are metadata-only and the sweep is gone, but
  the experiment reviewer's core job is checking the report against raw result files. Options: the daemon
  re-observes/re-hashes result resources at `review.start` and records the observation (recommended — keeps
  drift detectable exactly when it matters), vs explicitly downgrading result evidence to attestation.
  Dependency: the recommended option needs a mechanism the current architecture doesn't define —
  either a new `observe_resources` task type in §3.1 or reclassifying `review.start` as an aggregate
  route; the §3.3 routing table's `review.*` row carries that contingency.
- **F — Result-file viewing in the cloud UI:** degraded metadata-only state vs daemon-proxied reads.
  (A third option, opt-in result upload, would amend fixed decision 6's "result files stay
  metadata-only" and is out of scope unless that decision is reopened.)
- **G — Lambda dashboards in split mode:** daemon-owned loopback tunnels (default) vs cloud management-key
  HTTPS proxy.
- **H — Daemon lifetime:** continuous user service (recommended — reduces parachute frequency and keeps
  auto-sync alive) vs agent-session-scoped.
- **I — Sync tool surface:** keep one-shot `sandbox.sync` + lease fields in `sandbox.get` (current plan) vs
  the split doc's `start_sync`/`stop_sync`/`sync_status` set.
- **J — Audit log placement:** cloud-only per-tenant audit vs cloud + thin local mirror.
- **K — UI hosting:** cloud-served SPA only vs also supporting the local dev-proxy against the cloud API.

## 7. Test strategy (cross-cutting)

- **Dual-wiring contract suite** (`test_control_plane_contract.py`): one behavioral scenario corpus run
  against the in-process client (from Phase 3) and the HTTP client (from Phase 8), asserting identical
  results and error codes — the plane-seam analog of `test_sandbox_backend_contract.py`.
- **Plane-layout AST lints** (`test_plane_layout.py`): control modules can never import local-IO modules;
  tightened per phase, hard from Phase 3.
- **Inversion, not deletion:** every live-file-semantics test is rewritten in Phase 2 to assert the new
  semantics (`test_artifact_submission.py`), so coverage never lapses.
- **Dual-dialect CI** from Phase 6 (SQLite + Postgres); **dual-mode parity** from Phase 8 (local vs split,
  same scenario corpus, compared results); `test_local_shipping.py` green and unmodified at every phase.
- **Expiry coverage never lapses:** the reap/final-pull assertions are rewritten in the same diff as each
  reshape (Phase 4 task shape, Phase 5 parachute, Phase 9 cloud resume).
