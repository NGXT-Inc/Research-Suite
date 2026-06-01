# Modal Storage And Sync

This backend uses one project-scoped Modal Volume as the remote copy of the
repo. The Volume root mirrors the local repo root and is mounted writable into
each Modal sandbox at the configured remote workdir. Jobs run directly inside
that mounted repo; there is no read-only mount, copied workdir, or separate
remote output directory.

## Storage Surfaces

There are three storage surfaces:

- Local filesystem: the repo where the daemon, MCP server, and UI run.
- Modal Volume: the durable remote repo mirror for one project.
- Modal sandbox filesystem: the live container view where the Volume is mounted
  and job commands execute.

The Volume is the bridge between local and sandbox storage. The sandbox does not
write outputs through an API back to the daemon. It writes files into the mounted
Volume, and the daemon later synchronizes that Volume with the local repo.

## Sandbox To Volume

The remote runner writes status, logs, and job outputs inside the mounted repo.
Because those paths are on the mounted Volume, Modal's Volume commit machinery is
responsible for durability.

The runner flushes local filesystem buffers with `sync <mountpoint>`:

- when the job first enters `running`
- every 30 seconds while the job is running
- when the job exits successfully
- when the job fails
- when the job is cancelled
- when the runner catches an exception

The sandbox does not call `Volume.commit()` directly. The sandbox does not have
Modal credentials, and Modal already runs background commits plus a final commit
on container shutdown. The `sync` call just makes the sandbox's local writes
ready for Modal's commit process to pick up promptly.

The daemon calls `volume.reload()` before reading or scanning a Volume so its
handle can see writes committed by other containers.

## Volume To Local Filesystem

Local sync is handled by `SyncEngine`. Sync is always bidirectional: one pass
compares the local repo, the Modal Volume, and the last clean baseline, then
pushes, pulls, deletes, or records conflicts.

The baseline is durable SQLite state under `.research_plugin/modal/sync.sqlite`.
It records the last known clean local and remote fingerprints for each synced
path. If both local and remote changed the same path since the baseline, sync
records a conflict. Submitting a job refuses to proceed while unresolved sync
conflicts exist.

`expected_outputs` is only an availability hint. It is not a transfer list.
After a successful job, materialization triggers a normal bidirectional sync and
then checks whether the declared output paths now exist locally.

Some paths are excluded from normal repo sync, including internal plugin state,
runner state, virtualenvs, caches, `node_modules`, bytecode, and large
volume-managed data prefixes. Runner status and logs under `.research_plugin_job`
are intentionally excluded from normal repo sync; the backend can read them
directly from the committed Volume when live sandbox reads fail or the sandbox is
gone.

## When Sync Runs

Sync runs from both scheduled and event-driven paths:

- A background poller runs every 60 seconds over known projects.
- Submit performs an awaited sync before sandbox acquisition and runner start.
- Successful job materialization performs an awaited sync before checking output
  availability.

The background poller continues syncing while jobs are active. Failed and
cancelled jobs still write and flush terminal state to the Volume, but they do
not currently force an immediate local materialization sync; the poller or a
later manual/event-driven sync pulls those artifacts.

## Queueing And Backpressure

All sync callers use the same queueing system.

Per project, there can be at most one running sync and one queued sync. Manual
callers, such as submit and materialization, wait for a sync to happen before
continuing. If the running and queued slots are both full, a manual caller
coalesces onto the queued sync and receives that queued sync's result.

The poller uses skip-if-busy behavior. If both slots are full, the poller skips
that project for the current tick and tries again on the next interval.

Projects have independent in-process queues, but actual sync passes are
serialized by a repo-wide file lock because scanning, applying changes, and
writing the baseline all mutate the shared local repo.

## Concurrency Model

Multiple Modal jobs may run against the same project Volume at the same time.
This is intentional. If parallel jobs write different paths, sync pulls their
outputs normally. If parallel jobs write the same path, the latest committed
state wins. That last-writer-wins risk is accepted for this workflow.

Local edits can also race with remote job writes. The three-way baseline catches
local-vs-remote divergent edits as conflicts, but it is not a transactional
filesystem. The design favors throughput, bounded backpressure, and recoverable
conflict handling over strict serialization of all writes.
