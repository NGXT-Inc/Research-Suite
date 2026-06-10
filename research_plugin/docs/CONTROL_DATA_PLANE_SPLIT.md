# Future improvement: Control plane / data plane split for cloud, multi-user backend

**Status:** Proposal (not yet implemented) · **Drafted:** 2026-06-07

## Why this doc exists

Today the backend runs entirely on the user's machine. Both the long-lived
HTTP daemon (`python -m backend.http_server`) and the stdlib-only stdio proxy
(`python -m mcp_server`) are local, and the daemon happens to sit on the same
filesystem as the user's research repo. That co-location is the only reason
sync works: the daemon can `rsync` between a Modal/Lambda VM and a local path
like `experiments/<id>/synced/`.

When the backend moves to the cloud and serves multiple users, that assumption
breaks. This doc proposes splitting the monolith into a **cloud control plane**
and a **local data plane**, and pins down exactly which existing module lands on
which side.

## The load-bearing constraint

> **The cloud backend cannot see a user's local filesystem.**

A cloud-hosted backend has no access to `experiments/<id>/synced/`, the user's
repo files, or their SSH `known_hosts`. Therefore **any code that reads or
writes local files, or spawns processes that do (rsync, ssh, ssh-keygen), must
run in a process on the user's machine.** Everything else — orchestration,
records, credentials, authz — can and should move to the cloud.

This single rule determines the entire split.

## Target topology

Three roles instead of two:

```
┌──────────────────────────────────────────────────────────────────────┐
│  USER MACHINE                                                          │
│                                                                        │
│   Agent (Codex / Claude Code)                                          │
│        │ stdio                                                         │
│        ▼                                                               │
│   MCP server  ──────────── control-plane tools ──────────► CLOUD       │
│   (thin proxy)  ──── data-plane tools ───► Local data-plane daemon     │
│                                                 │                      │
│                                                 │ rsync / ssh          │
│                                   reads/writes  ▼                      │
│                              experiments/<id>/synced/, repo files,     │
│                              .research_plugin/ keys + state            │
│                                                 │                      │
│                                                 │ ssh ─────────────►   │  Modal / Lambda VM
│                                                 └── reports status ─►  │  CLOUD CONTROL PLANE
└──────────────────────────────────────────────────────────────────────┘

CLOUD (multi-tenant)
   Control plane: auth, ownership, project/experiment/claim/review records,
   sandbox lifecycle, provider credentials + billing, SSH credential issuance,
   sync-session + lease authority, status aggregation, cleanup jobs.
```

- **Cloud control plane** — multi-tenant, the source of truth for orchestration
  and records. Provisions VMs, but never touches a user's filesystem.
- **Local data-plane daemon** — one long-lived process per user machine. Has
  filesystem access; runs rsync/watch; authenticates to the cloud as the user.
  This is the role the **current HTTP daemon already plays for sync** — we keep
  that half and shed the rest to the cloud.
- **MCP server** — stays a thin, stateless stdio proxy. It gains a second
  upstream: control-plane tool calls go to the cloud, data-plane tool calls go
  to the local daemon.

### Key recommendation: do **not** move the sync worker into the MCP server

The MCP server is deliberately stdlib-only and stateless. It is spawned and
killed by the agent client, and is typically one process per client session.
Hosting a long-lived sync worker there means (a) sync dies when the editor
closes, and (b) two editor windows = two workers fighting over the same
experiment folder. Keep the worker in a single per-machine daemon; let MCP
processes be thin clients to it. **We already have that daemon — keep it,
shrink it to data-plane-only.**

## The split, module by module

The current composition root is [`backend/app.py`](../backend/app.py), which
wires the services below. Here is where each lands.

### Stays in the cloud (control plane)

| Component | Module today | Why it's cloud |
|---|---|---|
| Projects | `services/projects.py` | Pure records + ownership; no local FS. |
| Claims | `services/claims.py` | Records. |
| Experiments + state machine | `services/experiments.py` | Records + transition rules. |
| Reviews | `services/reviews.py` | Records + reviewer capabilities. |
| Workflow orchestration | `services/workflow.py` | `status_and_next` is pure logic over records. |
| Permissions / authz | `services/permissions.py` | Becomes the real multi-tenant authz layer. |
| Compute catalog | `services/compute.py` | GPU/pricing metadata. |
| Sandbox **lifecycle records + provisioning** | `services/sandboxes.py` (provision/terminate/reconcile, lifecycle rows) | Calls Modal/Lambda; holds provider creds; no FS needed. |
| Execution backends | `execution/backends/{modal,lambda_labs}` | Provider credentials + VM API calls belong server-side. |
| Provider credentials + billing | (Modal/Lambda config) | Must never sit on user machines in a multi-tenant world. |
| Durable state | `state/store.py` | Becomes the multi-tenant DB (Postgres), keyed on user/project, not `repo_root`. |
| Audit / activity | `state/activity.py`, `state/tool_calls.py` | Cloud-side audit per tenant (a thin local mirror is optional). |

### Must run locally (data plane)

| Component | Module today | Why it's local |
|---|---|---|
| **rsync transfer** | [`execution/ssh_rsync.py`](../backend/execution/ssh_rsync.py) | Reads/writes the local experiment folder; spawns `rsync`/`ssh`. |
| **Auto-sync poller + per-experiment sync locks** | `services/sandboxes.py` `_auto_sync_loop` / `_sync_row` / `_push_initial_files` | Drives the local rsync; must be near the files. |
| **Local sync directory layout** | [`execution/sync_dirs.py`](../backend/execution/sync_dirs.py) `local_experiment_sync_dir` | `experiments/<id>/synced/` is a local path. |
| **SSH keypair material on disk** | `services/sandbox_conn.py` `SandboxConnFiles.ensure_keypair` (ssh-keygen → `.research_plugin/sandboxes/keys`) | Private key stays on the user's machine (see credential model below). |
| **Sandbox dispatcher + conn files** | `services/sandbox_conn.py` (`.research_plugin/sbx`, `conn/<id>`) | Local helper the agent shells out to. |
| **Resource file observation** | `services/resources.py` `register_file` (single `path` or `paths` batch) | Hashes/reads **repo-relative local files**; only the resulting metadata is cloud state. |
| **Local rsync binary resolution** | `execution/ssh_rsync.py` `resolve_rsync` | Inspects the local machine's installed rsync (the reason this doc's sibling fix exists). |
| **Daemon discovery marker** | `daemon_marker.py`, `.research_plugin/daemon.json` | Local process discovery. |

### Splits across the seam

A few responsibilities are genuinely two-sided. The **bytes/IO half is local;
the record/metadata half is cloud.**

- **Sandbox sync.** Cloud sets up the remote `/workspace/synced` contract and
  tracks status/last-sync metadata; the local daemon moves the bytes.
- **Resources.** Local daemon reads the file and computes the version hash;
  cloud stores the resource record and immutable version history.
- **SSH access.** Cloud authorizes access and owns credential validity/rotation;
  local daemon holds the private key and runs the `ssh`/`rsync` client.
- **Tenancy routing.** Today [`project_router.py`](../backend/project_router.py)
  multiplexes a shared daemon into per-`repo_root` app instances — a local,
  directory-keyed primitive. In production, **tenancy (user/project) moves to
  the cloud**, while the local daemon keeps the directory mapping (`repo_root` ↔
  experiment folders). Anything keyed on `repo_root` is, by definition, local.

## The seam: contracts between cloud and local

### Sync session (cloud → local)

When the agent procures a sandbox, the cloud returns a **sync session** the
local daemon acts on:

```jsonc
{
  "experiment_id": "...",
  "sandbox_id": "...",
  "ssh": { "host": "...", "port": 22, "user": "root",
           "credential": "<short-lived cert or ephemeral key ref>" },
  "remote": { "synced": "/workspace/synced",
              "unsynced": "/workspace/unsynced",
              "artifacts_to_keep": "/workspace/synced/artifacts_to_keep" },
  "lease": { "id": "...", "ttl_seconds": 120, "holder_client_id": "..." },
  "direction_policy": {
    // per-subtree authority — closes the --delete footgun
    "synced": "remote_authoritative_for_results",
    "artifacts_to_keep": "remote_append_only"
  }
}
```

### Lease authority lives in the cloud

The lease is the **only** safe place to coordinate multiple local clients,
because the cloud is the only thing all of them can see. A lease is
`{experiment_id, holder_client_id, ttl}`, renewed by the holding daemon. If the
daemon dies the lease expires and another client can claim it. **Do not attempt
peer-to-peer lease coordination between local processes.** This directly
addresses the "two local clients fight over one experiment" problem.

### MCP tool surface (lease-aware)

Sync tools must not block the handler. They control/observe a background worker
owned by the local daemon:

- `sandbox.start_sync` — acquire the cloud lease, start the local worker;
  if already held, report the current holder instead of racing.
- `sandbox.stop_sync` — stop the worker, release the lease.
- `sandbox.sync_once` — one-shot pull/push (today's `sandbox.sync`).
- `sandbox.sync_status` — lease holder, last sync time + direction, failure
  count, active local client id.

## Cross-cutting concerns to design before this is real

1. **Local → cloud auth bootstrap.** The local daemon must authenticate to the
   cloud *as the user* so ownership checks mean anything. Device-flow OAuth →
   local refresh token → exchange for short-lived sync-session credentials.
   This is the foundation everything else rests on.

2. **SSH credential model.** Prefer an **SSH CA**: the local daemon generates a
   keypair (private key never leaves the machine), the cloud signs the public
   key into a short-TTL certificate scoped to one sandbox. This keeps the
   private key local *and* puts validity/revocation/rotation in the control
   plane. (Today `_ensure_keypair` generates a long-lived local key and hands
   the public key to the backend; the CA model is the production evolution.)

3. **`--delete` + multi-client is a footgun.** Current sync pulls with
   `--delete`. Across a network boundary with leases, a stale client can wipe
   live work. The `direction_policy` in the sync session must specify per-subtree
   authority explicitly; the local syncer enforces it.

4. **Provider-credential ownership — a fork, not a footnote.**
   - *Platform-owned* Modal/Lambda accounts with per-user billing attribution:
     best UX, but you become a compute reseller (abuse/quota/billing risk).
   - *Bring-your-own*, user-scoped, encrypted at rest: no fronted spend, worse
     UX. Pick deliberately.

5. **Cleanup jobs for abandoned VMs.** With provisioning server-side, the cloud
   must reap VMs whose lease/owner has gone away (today reconciliation is
   best-effort and local).

6. **Per-sandbox isolation.** One namespace per user/experiment; per-sandbox SSH
   credentials, never a shared global key.

## Why direct local↔VM, not a cloud relay

An alternative is routing bytes user → cloud blob store → VM, so the cloud can
"see" the data. We reject it as the default: it doubles byte movement, adds
storage cost, and *still* needs a local agent to push from the filesystem — so
it doesn't remove the local component. Direct local↔VM SSH is simpler and
cheaper for large artifacts. The one reason to revisit is unreachable VMs
(NAT/firewall); Modal/Lambda VMs are generally directly reachable, so direct
stays the default with relay as a fallback transport.

## Suggested migration path (incremental)

This is evolution, not a rewrite — the local daemon already owns sync.

1. **Carve the seam in-process first.** Split `SandboxService` into a
   control half (lifecycle records, provisioning) and a data half (sync worker,
   keys, local dirs) behind an interface, while both still run locally. No
   behavior change; just a clean boundary.
2. **Define the sync-session + lease contract** (above) and route today's
   `sandbox.sync` through it locally.
3. **Stand up the cloud control plane** (auth, multi-tenant DB, provisioning,
   credential issuance). Point the MCP server's control-plane tools at it.
4. **Ship the local data-plane daemon** as the slimmed-down successor to
   `backend.http_server`: data half only, authenticating to the cloud.
5. **Add lease enforcement + `direction_policy`** and the
   `start_sync`/`stop_sync`/`sync_status` tools.

## Open decisions

- SSH CA vs. ephemeral-keypair-per-session (recommend CA).
- Platform-owned vs. bring-your-own provider credentials.
- Does the local daemon run continuously (background sync even with no agent
  session) or only while an agent is connected?
- Where the activity/audit log lives — cloud-only, or cloud with a local mirror
  for offline debugging.
- One local daemon per machine vs. per-user on shared machines.

## Related

- [`STARTUP_CHEATSHEET.md`](STARTUP_CHEATSHEET.md) — current process topology
  (daemon vs. MCP proxy).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — current component architecture.
- [`execution/ssh_rsync.py`](../backend/execution/ssh_rsync.py) — the local
  rsync transfer that anchors the data plane.
</content>
