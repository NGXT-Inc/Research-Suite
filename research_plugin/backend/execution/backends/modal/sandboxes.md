# Sandbox Execution Model (SSH, no jobs)

This backend gives the agent **direct access to a Modal sandbox over SSH**. There
is no job abstraction: the agent requests a sandbox for an experiment, receives
SSH connection details, and runs ordinary shell commands itself. The plugin's
role is to procure, track, and shut down sandboxes while recording what
happened for user visibility.

## Concepts

- **Experiment** is the default attachment context. An experiment may have
  multiple live sandboxes, and a running sandbox can be attached to another
  ready/running experiment.
- **Sandbox registry** (`SandboxService`) is the central authority for
  procurement, status, SSH access, and shutdown. It owns the durable
  `sandboxes` table and the sandbox SSH keypair.
- **Sandbox backend** (`ModalSandboxBackend`) owns the Modal mechanics: create a
  sandbox, wire SSH, check liveness, terminate, and read the transcript.
- **Workspace convention** is provider-neutral. The experiment's remote work
  folder is `/workspace/<name>` and `/workspace/data` is the conventional
  scratch home for datasets/caches. Nothing is copied back automatically:
  agents explicitly copy light retained files over SSH or upload heavy outputs
  with storage tools before release.

## Procurement: request, reuse, or attach

`sandbox.request(project_id, experiment_id, gpu?, cpu?, memory?, time_limit?)`:

1. Look up an active sandbox attached to the experiment.
2. If a matching row exists and the Modal sandbox is still alive, return its stored SSH
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
    workdir="/workspace/<name>",
    unencrypted_ports=[22],
    env={
        "RP_AUTHORIZED_KEY": public_key,
        "RP_EXPERIMENT_ID": experiment_id,
        "RP_WORKDIR": "/workspace/<name>",
        "RP_EXPERIMENT_DIR": "/workspace/<name>",
        "RP_SANDBOX_DATA_DIR": "/workspace/data",
        "RP_SESSION_DIR": "/workspace/.research_plugin_sessions/<experiment_id>",
    },
)
host, port = sandbox.tunnels()[22].tcp_socket
```

The keypair is generated and owned by the registry for the sandbox. Daemon and agent share a host,
so the registry returns a ready-to-run command:

```bash
ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null root@<host> '<your shell command>'
```

Use `$RP_EXPERIMENT_DIR` for scripts, configs, compact outputs, reports, and
figures that may need to be retained. Use `$RP_DATASET_DIR` (or anywhere
outside the experiment folder) for large datasets, caches, checkpoints, and
temporary derived data. Before release, copy light retained files into the
local experiment folder or upload heavy outputs with storage tools.

Agents should use CPU-only sandboxes for dataset inspection and data engineering
unless the command needs GPU acceleration. They can request more RAM with
`memory` in MiB and more CPU with `cpu` in Modal CPU cores.

If the backend env file or process environment contains `HF_TOKEN`, sandbox
creation passes it through with Modal's `secrets` API. The SSH wrapper exports
both `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` for Hugging Face tooling. The token
value must not be written into retained files, transcripts, resources, or
agent-visible API responses.

## Image additions

The base image installs the baseline agent tooling plus two scripts:

- `/opt/rp/boot.sh` writes `$RP_AUTHORIZED_KEY`, creates the experiment dir,
  `/workspace/data`, `artifacts_to_keep/`, and the sessions dir, then
  `exec`s `sshd -D`.
- `/opt/rp/rec.sh` is the `ForceCommand` transcript wrapper.

## Visibility: the transcript wrapper

`sshd` is configured with `ForceCommand /opt/rp/rec.sh`. Every SSH channel is
recorded to:

```bash
$RP_SESSION_DIR/transcript.log
```

The wrapper records commands, streams stdout/stderr back to the SSH channel, and
preserves the real command exit status. `sandbox.terminal` reads the transcript
live from the running sandbox.

## Training observability: centralized MLflow

Every sandbox installs the MLflow client package, but sandbox provisioning does
not automatically export tracking env vars. Agents get the central tracking
URI and experiment name from `mlflow.context` or
`experiment.transition(start_running)`, then set those env vars in the SSH
command that runs training. The sandbox does not run an MLflow tracking server,
TensorBoard server, or sandbox-local dashboard tunnel.

## Shutdown / status

- `sandbox.release(project_id, experiment_id)` terminates the Modal sandbox and
  marks the row `terminated`. The VM filesystem is ephemeral; agents must copy
  out or upload any files they want to keep before release.
- `time_limit` is the Modal sandbox `timeout`; Modal reaps the container when it
  elapses. `sandbox.get` refreshes liveness and reconciles the row to
  `terminated` when Modal says the sandbox is gone.
- Sandboxes are tagged with `research_plugin`, `project_id`, `experiment_id`,
  and sandbox role so a daemon restart can rediscover them.

## Tool surface (agent-facing)

| Tool | Purpose |
|------|---------|
| `sandbox.request` | Procure or reuse the experiment's sandbox; returns SSH details and remote/local path guidance. |
| `sandbox.get` | Current sandbox status + SSH details for the experiment. |
| `sandbox.list` | All experiment sandboxes in the project. |
| `sandbox.release` | Terminate the experiment's sandbox after confirming needed files were retained. |
| `sandbox.terminal` | Read the experiment's terminal transcript tail. |
| `sandbox.health` | Is the execution backend reachable. |

The agent's normal loop is: `sandbox.request` -> run/edit/write files over SSH
in `$RP_EXPERIMENT_DIR` (heavy files outside it) -> copy light retained files
out over SSH or upload heavy files to durable storage -> register/associate
resources -> transition to review.
