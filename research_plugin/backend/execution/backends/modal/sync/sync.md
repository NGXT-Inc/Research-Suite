# Modal Storage And Sync

This backend uses one project-scoped Modal Volume as the remote copy of the
repo. The Volume root mirrors the local repo root and is mounted writable into
each Modal sandbox at the configured remote workdir. The agent's commands run
directly inside that mounted repo over SSH; there is no read-only mount, copied
workdir, or separate remote output directory.

## Storage Surfaces

There are three storage surfaces:

- Local filesystem: the repo where the daemon, MCP server, and UI run.
- Modal Volume: the durable remote repo mirror for one project.
- Modal sandbox filesystem: the live container view where the Volume is mounted
  and the agent's SSH commands execute.

The Volume is the bridge between local and sandbox storage. The sandbox does not
write outputs through an API back to the daemon. It writes files into the mounted
Volume, and the daemon later synchronizes that Volume with the local repo.

## Sandbox To Volume

The agent's SSH commands write outputs and a terminal transcript inside the
mounted repo. Because those paths are on the mounted Volume, Modal's Volume
commit machinery is responsible for durability.

The in-sandbox `sshd` `ForceCommand` transcript wrapper flushes local filesystem
buffers with `sync <mountpoint>` after each command it records. This keeps the
Volume copy of outputs and the transcript fresh for Modal's commit process.

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
records a conflict. `sandbox.request` refuses to proceed while unresolved sync
conflicts exist.

Some paths are excluded from normal repo sync, including internal plugin state,
session transcripts, virtualenvs, caches, `node_modules`, bytecode, and large
volume-managed data prefixes. Terminal transcripts under
`.research_plugin_sessions` are intentionally excluded from normal repo sync; the
backend reads them directly from the live sandbox (or the committed Volume when
the sandbox is gone).

## When Sync Runs

Sync runs from both scheduled and event-driven paths:

- A background poller runs every 60 seconds over known projects.
- `sandbox.request` performs an awaited push of the current repo before the
  sandbox boots, so the agent sees up-to-date code.

The background poller continues syncing while a sandbox is active, pulling the
agent's outputs back to the local repo on each tick.

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

Multiple experiment sandboxes may run against the same project Volume at the same
time. This is intentional. If they write different paths, sync pulls their
outputs normally. If they write the same path, the latest committed state wins.
That last-writer-wins risk is accepted for this workflow.

Local edits can also race with remote sandbox writes. The three-way baseline catches
local-vs-remote divergent edits as conflicts, but it is not a transactional
filesystem. The design favors throughput, bounded backpressure, and recoverable
conflict handling over strict serialization of all writes.
