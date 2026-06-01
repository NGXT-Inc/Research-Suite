# Sandbox Execution Model (SSH, no jobs)

This backend gives the agent **direct access to a Modal sandbox over SSH**. There
is no "job" abstraction: the agent requests a sandbox for an experiment, receives
SSH connection details, and runs ordinary shell commands itself. The plugin's
role is to *procure, track, and shut down* sandboxes and to *record what happened*
so the user keeps visibility.

Two design pillars:

1. **Easiest for the agent.** Agents are good at running scripts and shell
   commands. So we hand them exactly that: a live machine and an `ssh` command.
   No bespoke submit/poll/materialize protocol to learn.
2. **Visibility without a leash.** The agent runs whatever it wants over SSH, but
   every command and its output is recorded to a per-experiment transcript that
   the UI renders as a live terminal window.

## Concepts

- **Experiment** is the unit of execution. An experiment has **at most one live
  sandbox** at a time.
- **Sandbox registry** (`SandboxService`) is the central authority for
  procurement, status, and shutdown. It owns the durable `sandboxes` table (one
  row per experiment) and the per-experiment SSH keypair.
- **Sandbox backend** (`ModalSandboxBackend`) owns the Modal mechanics: create a
  sandbox, wire SSH, check liveness, terminate, and read the transcript.
- **Project Volume** is unchanged: one Modal Volume per project, mirroring the
  repo, mounted writable into every sandbox. Bidirectional sync (`SyncEngine`)
  still reconciles the Volume with the local repo. See `sync/sync.md`.

## Procurement: one sandbox per experiment, reuse-if-alive

`sandbox.request(project_id, experiment_id, gpu?, cpu?, memory?, time_limit?)`:

1. Look up the experiment's current sandbox row.
2. If a row exists and the Modal sandbox is **still alive**, return its stored SSH
   details (`reused: true`). The tunnel host/port are stable for the sandbox's
   lifetime, so the cached details remain valid.
3. Otherwise **create a fresh sandbox**, wire SSH, persist the row, and return the
   new details (`reused: false`).

Procurement is the registry's job, not the agent's. The agent always calls
`sandbox.request`; whether it gets a reused or fresh sandbox is transparent.

## SSH wiring

SSH over Modal uses an **unencrypted TCP tunnel** (TLS is wrong for SSH):

```python
sandbox = modal.Sandbox.create(
    "/opt/rp/boot.sh",            # entrypoint: authorize key, start sshd -D
    app=app, image=image, gpu=gpu, cpu=cpu, memory=memory,
    timeout=time_limit, workdir=remote_workdir,
    volumes={remote_workdir: volume},
    unencrypted_ports=[22],
    secrets=[modal.Secret.from_dict({
        "RP_AUTHORIZED_KEY": public_key,
        "RP_EXPERIMENT_ID": experiment_id,
        "RP_WORKDIR": remote_workdir,
    })],
)
host, port = sandbox.tunnels()[22].tcp_socket
```

The **keypair is generated and owned by the registry**, per experiment, under
`.research_plugin/sandboxes/keys/<experiment_id>`. Daemon and agent share a host,
so the registry returns a ready-to-run command:

```
ssh -i <key_path> -p <port> -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null root@<host> '<your shell command>'
```

Reuse works because the same per-experiment public key stays authorized across
the sandbox's life.

### Image additions

The base image installs `openssh-server` and bakes two scripts:

- `/opt/rp/boot.sh` — entrypoint. Writes `$RP_AUTHORIZED_KEY` to
  `~/.ssh/authorized_keys`, generates host keys, writes an `sshd_config` whose
  `ForceCommand` is the transcript wrapper, then `exec`s `sshd -D` (which keeps
  the container alive and serving SSH).
- `/opt/rp/rec.sh` — the `ForceCommand` transcript wrapper (below).

## Visibility: the transcript wrapper

`sshd` is configured with `ForceCommand /opt/rp/rec.sh`. Every SSH channel —
interactive shell or `ssh host 'cmd'` — is funneled through it. The wrapper:

```bash
LOG="$RP_WORKDIR/.research_plugin_sessions/$RP_EXPERIMENT_ID/transcript.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if [ -n "$SSH_ORIGINAL_COMMAND" ]; then
  printf '\n[%s] $ %s\n' "$(ts)" "$SSH_ORIGINAL_COMMAND" >> "$LOG"
  bash -lc "$SSH_ORIGINAL_COMMAND" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  printf '[%s] (exit %d)\n' "$(ts)" "$rc" >> "$LOG"
  sync "$RP_WORKDIR" 2>/dev/null || true   # nudge Modal's volume commit
  exit $rc
else
  printf '\n[%s] (interactive shell)\n' "$(ts)" >> "$LOG"
  exec bash -l
fi
```

- The agent still sees command output (it's tee'd back to the SSH channel).
- The transcript captures both stdout and stderr with timestamps and exit codes.
- It lives on the mounted Volume, so it survives sandbox death.
- `tee` preserves the real exit code via `${PIPESTATUS[0]}` so the agent's `ssh`
  exit status is honest.

### Reading the transcript

`sandbox.terminal(project_id, experiment_id, tail?)` and the UI read the
transcript **live first, durable second**:

1. **Live**: `sandbox.exec("tail", "-c", N, transcript)` against the running
   sandbox — no commit latency.
2. **Fallback**: `volume.read_file(rel_path)` reads the last committed copy when
   the live read fails or the sandbox is already reaped.

The transcript path (`.research_plugin_sessions/`) is excluded from normal repo
sync — it is operational state, read directly from the sandbox/Volume.

## Shutdown / status

- **Explicit**: `sandbox.release(project_id, experiment_id)` terminates the Modal
  sandbox and marks the row `terminated`.
- **Time limit**: `time_limit` is the Modal sandbox `timeout`; Modal reaps the
  container when it elapses. `sandbox.get` refreshes liveness and reconciles the
  row to `terminated` when Modal says the sandbox is gone.
- **Tags**: sandboxes are tagged with `research_plugin`, `project_id`,
  `experiment_id`, `sandbox role` so a daemon restart can rediscover them via
  `Sandbox.list(tags=…)` even if the local row is lost.

## Tool surface (agent-facing)

| Tool | Purpose |
|------|---------|
| `sandbox.request` | Procure (reuse-or-create) the experiment's sandbox; returns SSH details. |
| `sandbox.get` | Current sandbox status + SSH details for the experiment. |
| `sandbox.list` | All experiment sandboxes in the project. |
| `sandbox.release` | Terminate the experiment's sandbox. |
| `sandbox.terminal` | Read the experiment's terminal transcript (tail). |
| `sandbox.health` | Is the execution backend reachable. |

The agent's normal loop is: `sandbox.request` → run commands over SSH → sync
result resources → `experiment.transition` to review. No job lifecycle to manage.

## What changed from the job model

- **Removed**: `job.*` tools, `JobService`, the detached runner protocol, submit
  pipeline stages, output materialization, `expected_outputs` transfer semantics,
  and the `jobs` table. Backend `submit/status/logs/cancel/materialize` are gone.
- **Kept**: the project Volume, `SyncEngine`/`SyncPoller` (repo ↔ Volume), Modal
  app/image/tags machinery, and recovery-by-tag.
- **Replaced**: the UI's job cards/dashboard with a per-experiment terminal view.

## Non-goals

- No re-introduction of a server-side command queue. The agent drives execution.
- No interception of the agent's SSH traffic by the daemon — visibility comes
  from the in-sandbox transcript, not a proxy.
- The backend stays Modal-specific; SSH-to-sandbox is not a portable abstraction.
