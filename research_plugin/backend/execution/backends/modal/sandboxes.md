# Sandbox Execution Model (SSH, no jobs)

This backend gives the agent **direct access to a Modal sandbox over SSH**. There
is no job abstraction: the agent requests a sandbox for an experiment, receives
SSH connection details, and runs ordinary shell commands itself. The plugin's
role is to procure, track, sync, and shut down sandboxes while recording what
happened for user visibility.

## Concepts

- **Experiment** is the unit of execution. An experiment has at most one live
  sandbox at a time.
- **Sandbox registry** (`SandboxService`) is the central authority for
  procurement, status, provider-neutral SSH rsync, and shutdown. It owns the
  durable `sandboxes` table and the per-experiment SSH keypair.
- **Sandbox backend** (`ModalSandboxBackend`) owns the Modal mechanics: create a
  sandbox, wire SSH, check liveness, terminate, and read the transcript.
- **Sync contract** is provider-neutral. The remote synced directory is
  `/workspace/synced`; the remote unsynced directory is `/workspace/unsynced`.
  See `sync/sync.md`.

## Procurement: one sandbox per experiment, reuse-if-alive

`sandbox.request(project_id, experiment_id, gpu?, cpu?, memory?, time_limit?)`:

1. Look up the experiment's current sandbox row.
2. If a row exists and the Modal sandbox is still alive, return its stored SSH
   details (`reused: true`).
3. Otherwise create a fresh sandbox, wire SSH, persist the row, and return the
   new details (`reused: false`).

Procurement is the registry's job, not the agent's. The agent always calls
`sandbox.request`; whether it gets a reused or fresh sandbox is transparent.

## SSH wiring

SSH over Modal uses an unencrypted TCP tunnel because SSH already provides its
own transport security:

```python
sandbox = modal.Sandbox.create(
    "/opt/rp/boot.sh",
    app=app,
    image=image,
    gpu=gpu,
    cpu=cpu,
    memory=memory,
    timeout=time_limit,
    workdir="/workspace/synced",
    unencrypted_ports=[22],
    env={
        "RP_AUTHORIZED_KEY": public_key,
        "RP_EXPERIMENT_ID": experiment_id,
        "RP_WORKDIR": "/workspace/synced",
        "RP_SYNC_DIR": "/workspace/synced",
        "RP_UNSYNCED_DIR": "/workspace/unsynced",
        "RP_SANDBOX_DATA_DIR": "/workspace/unsynced",
    },
)
host, port = sandbox.tunnels()[22].tcp_socket
```

The keypair is generated and owned by the registry, per experiment, under
`.research_plugin/sandboxes/keys/<experiment_id>`. Daemon and agent share a host,
so the registry returns a ready-to-run command:

```bash
ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null root@<host> '<your shell command>'
```

Use `$RP_SYNC_DIR` for files that should sync back locally. Use
`$RP_UNSYNCED_DIR` / `$RP_DATASET_DIR` for large datasets, caches, checkpoints,
and temporary derived data. Put deliberately preserved large artifacts under
`$RP_SYNC_DIR/artifacts_to_keep`.

Agents should use CPU-only sandboxes for dataset inspection and data engineering
unless the command needs GPU acceleration. They can request more RAM with
`memory` in MiB and more CPU with `cpu` in Modal CPU cores.

If the backend env file or process environment contains `HF_TOKEN`, sandbox
creation passes it through with Modal's `secrets` API. The SSH wrapper exports
both `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` for Hugging Face tooling. The token
value must not be written into synced files, transcripts, resources, or
agent-visible API responses.

## Image additions

The base image installs the baseline agent tooling plus two scripts:

- `/opt/rp/boot.sh` writes `$RP_AUTHORIZED_KEY`, creates `/workspace/synced`,
  `/workspace/unsynced`, and `/workspace/synced/artifacts_to_keep`, starts
  observability servers, then `exec`s `sshd -D`.
- `/opt/rp/rec.sh` is the `ForceCommand` transcript wrapper.

## Visibility: the transcript wrapper

`sshd` is configured with `ForceCommand /opt/rp/rec.sh`. Every SSH channel is
recorded to:

```bash
$RP_SYNC_DIR/.research_plugin_sessions/$RP_EXPERIMENT_ID/transcript.log
```

The wrapper records commands, streams stdout/stderr back to the SSH channel, and
preserves the real command exit status. `sandbox.terminal` reads the transcript
live from the running sandbox.

## Training observability: MLflow + TensorBoard

Every sandbox also runs two observability servers:

- MLflow tracking server on port `5000`, backed by
  `$RP_SYNC_DIR/.research_plugin_sessions/<experiment_id>/mlflow.db`.
- TensorBoard on port `6006`, with `--logdir $RP_TB_LOGDIR`.

Both ports ship as Modal encrypted tunnels, so the daemon receives HTTPS URLs
via `sandbox.tunnels()[port].url`. The dashboard servers are best-effort: a
missing package or port collision loses observability for the run, never SSH.

## Shutdown / status

- `sandbox.release(project_id, experiment_id)` attempts a final rsync, then
  terminates the Modal sandbox and marks the row `terminated`.
- `time_limit` is the Modal sandbox `timeout`; Modal reaps the container when it
  elapses. `sandbox.get` refreshes liveness and reconciles the row to
  `terminated` when Modal says the sandbox is gone.
- Sandboxes are tagged with `research_plugin`, `project_id`, `experiment_id`,
  and sandbox role so a daemon restart can rediscover them.

## Tool surface (agent-facing)

| Tool | Purpose |
|------|---------|
| `sandbox.request` | Procure or reuse the experiment's sandbox; returns SSH details and synced/unsynced paths. |
| `sandbox.get` | Current sandbox status + SSH details for the experiment. |
| `sandbox.sync` | Pull `$RP_SYNC_DIR` to the experiment's local sync directory with SSH rsync. |
| `sandbox.list` | All experiment sandboxes in the project. |
| `sandbox.release` | Final best-effort sync, then terminate the experiment's sandbox. |
| `sandbox.terminal` | Read the experiment's terminal transcript tail. |
| `sandbox.health` | Is the execution backend reachable. |

The agent's normal loop is: `sandbox.request` -> run/edit/write files over SSH
in `$RP_SYNC_DIR` and `$RP_UNSYNCED_DIR` -> `sandbox.sync` ->
register/associate local result resources -> transition to review.
