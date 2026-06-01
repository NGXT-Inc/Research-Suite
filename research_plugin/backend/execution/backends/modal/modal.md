# Modal Execution Approach

This backend runs the existing `JobSpec` contract on remote Modal GPU workers
without exposing Modal-specific behavior to `JobService`, workflow tools, or
Codex. Codex should keep using the backend-neutral `job.*` MCP tools.

## Design Decisions

### Keep Modal Behind The Execution Contract

Modal is an implementation detail of the execution backend. The MCP surface,
workflow gates, resource model, review model, and job persistence stay
backend-neutral. Backend-specific settings may enter through opaque job hints or
daemon environment, but credentials never come from `JobSpec.env`.

### Use One Project Volume As The Remote Repo

Each project gets a Modal Volume that mirrors the local repo. The Volume is
mounted writable into every sandbox, and jobs run directly inside that mounted
repo. There is no read-only mount, no copied workdir, and no second remote
output directory.

This keeps remote execution close to local repo semantics: imports, configs,
experiment code, and generated outputs all live in the same tree. The tradeoff
is that sync must be disciplined, because the remote worker can write anywhere
that is not explicitly excluded.

### Make Sync A Separate Subsystem

Synchronization is separate from sandbox execution. The sync engine compares the
local repo, the Modal Volume, and the last clean baseline, then pushes, pulls,
or deletes files bidirectionally. If both sides changed the same path, sync
records a conflict and submit refuses to start until the conflict is resolved.

`expected_outputs` is only an availability hint for workflow/reporting. It is
not a transfer list. After a terminal job, the backend runs sync once and then
checks whether the declared outputs exist locally.

### Preserve Same-Repo Semantics

The Modal job should see the current experiment, sibling experiments, shared
code, shared configs, and shared inputs unless those paths are globally
excluded. We accept the larger sync surface because it avoids surprising import
and dependency behavior.

Large or reproducible data that should live only on the Modal Volume should be
excluded explicitly from normal bidirectional sync.

### Keep Submit Nonblocking

Submitting a job should allocate or reuse a sandbox, write a small runner
protocol into the mounted repo, launch the runner as a detached process, and
return a reconnectable `runtime_job_id`. The MCP call must not stay open for the
duration of training.

The detached runner owns the command process, status transitions, bounded logs,
cancellation sentinel, timeout handling, and final status write. It flushes the
mounted repo so Modal's Volume commit machinery can make status, logs, and
outputs visible to the daemon after reload.

### Accept Shared-Volume Concurrency

Multiple Modal jobs may run against the same project Volume at the same time.
The poller continues normal bidirectional sync while jobs are active. If parallel
jobs write the same path, the latest committed state wins; that risk is accepted
in exchange for throughput and simpler scheduling.

### Make Runtime Identity Recoverable

The runtime job id must contain enough information to reconnect after daemon or
MCP process restarts. Modal sandboxes should also be tagged with the research
job, experiment, and project identity so a submit that died after sandbox create
can recover the sandbox handle later.

### Prefer Correctness Over Sandbox Reuse

Retained sandboxes may be reused for quick retries when they belong to the same
experiment and have compatible execution requirements. If compatibility is
uncertain, create a fresh sandbox. Reuse is an optimization; it must never
change job semantics.

### Retain Workers Briefly After Terminal Results

Successful, failed, and cancelled jobs keep their sandbox alive for a short
debug/retry window before best-effort termination. This gives the agent time to
inspect logs or rerun without paying the full provisioning cost again.

Retained sandboxes need both in-process timers and tag-based cleanup, because a
daemon restart can lose local timer state while the Modal sandbox continues to
exist.

### Fall Back To Durable Volume State

Live sandbox reads are convenient but not authoritative enough by themselves.
If sandbox filesystem reads fail, time out, or the sandbox has already been
reaped, status and logs should fall back to the files committed in the project
Volume. If Modal says the sandbox terminated and no terminal status exists in
the Volume, report the job as failed rather than leaving it stuck as queued.

## Non-Goals

- Do not expose Modal file or package-management operations as model-facing
  tools.
- Do not provision SSH access into Modal sandboxes.
- Do not couple this backend to the chat sandbox runtime, user sessions,
  Supabase persistence, or frontend streaming concerns.
- Do not add Modal-specific workflow or review state outside the execution
  backend boundary.
