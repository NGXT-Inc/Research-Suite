# Modal Sandbox Backend

Modal is one implementation of the provider-neutral `SandboxBackend` contract.
The agent-facing lifecycle is shared with Lambda Labs and Thunder Compute; only
procurement mechanics and capability flags vary by provider.

## Architecture and ownership

```text
Agent client --stdio--> local MCP proxy --HTTP--> brain / SandboxService
      |                       |                         |
      | SSH commands          | rsync retained files   | Modal API
      +-----------------------+-------------------------+--> Modal sandbox
```

- The brain's `SandboxService` owns durable sandbox rows, experiment
  attachments, reuse policy, quotas, provisioning state, reaping, and release.
- `ModalSandboxBackend` creates the container, exposes SSH, refreshes the
  endpoint, checks liveness, terminates the container, and reads provider-side
  transcript, usage, and run-receipt data.
- The local proxy owns checkout paths and `sandbox.pull_outputs`. The brain does
  not receive `repo_root` and never reads the checkout.
- In project-local MCP sessions, the proxy injects hidden `project_id` scope.
  Agent calls use `experiment_id` or `sandbox_uid`, not a caller-selected project
  id.

There is no agent-facing remote job API. Provisioning may run asynchronously
inside the brain, while long commands use the provider-neutral `merv_run` receipt
convention.

## Sandbox identity and workspace

A sandbox is a project-scoped machine identified by `sandbox_uid`. It may be
standalone, attached to multiple experiments, and left running while attachments
change. An experiment may have multiple live sandboxes by requesting an
additional machine.

The workdir is sandbox-owned, normally `/workspace/sandbox-<uid-prefix>`, and is
independent of experiment attachment. The compatibility variables
`$RP_WORKDIR` and `$MERV_EXPERIMENT_DIR` both point there. `$RP_DATASET_DIR` and
`$RP_SANDBOX_DATA_DIR` point to `/workspace/data` for caches, datasets,
checkpoints, and other ephemeral bulk data.

Nothing is synchronized automatically. Files on the sandbox disappear on
release or timeout unless the agent pulls compact evidence into the checkout or
uploads heavy files to configured durable storage.

## SSH and key custody

Modal exposes port 22 through an unencrypted TCP tunnel; SSH itself supplies
transport encryption and authentication. Caller and brain credentials have
distinct duties:

- The caller owns the user SSH keypair and supplies only its OpenSSH public key
  to `sandbox.request`. The corresponding private key stays on the caller's
  machine and is used for agent SSH and proxy-local rsync pulls.
- The brain owns separate management and provider credentials for operational
  transcript, metrics, secret-delivery, and lifecycle paths. Those paths never
  depend on the caller's private key, and brain credentials are never returned
  as the caller's key.

The split proxy returns SSH facts (`host`, `port`, `user`), not a registry-owned
private key or guaranteed ready-made command. The agent constructs and runs the
SSH command with its own key. File-transfer commands bypass the transcript tee
so rsync/scp protocols remain intact.

## Procurement and lifecycle

`sandbox.request` may create a standalone machine or attach it to an experiment.
When an attached live sandbox is reusable, the service returns it; pass
`additional=true` to request another machine for the same experiment.

Modal composes resources directly from `gpu`, `cpu`, and `memory`; omitting
`gpu` requests CPU-only execution. `sandbox.options` describes the accepted GPU
and compute choices. Unlike fixed-SKU providers, Modal does not return a
`needs_selection` instance-type gate.

Provisioning waits for a bounded interval. If the container or SSH endpoint is
not ready, `sandbox.request` returns `provisioning`; poll `sandbox.get` until it
reports `running` or `failed`. Repeating `sandbox.request` is not a poll.

`sandbox.attach` associates an existing running machine with another experiment
without changing its workdir, SSH endpoint, or lifetime. Modal fixes its timeout
when the container is created, so `sandbox.extend` may reject live extension.

Release is destructive and two-step. The first `sandbox.release` call returns a
retention checklist. Only `confirm_retained=true` terminates the container.
Modal also enforces the requested `time_limit`; expiry destroys the filesystem.

## Execution and observability

The SSH `ForceCommand` wrapper records commands and streamed output in a
sandbox-scoped transcript while preserving exit status. `sandbox.terminal`
supports cursor-based polling and the brain persists a compact `last_command`
snapshot for temporary read failures.

Use `merv_run <label> -- <command>` for work that must survive an SSH disconnect.
It writes receipts under the sandbox workdir; the brain mirrors them so
`sandbox.runs` remains queryable after the machine is gone. Logs and output files
do not become durable merely because the receipt does.

The image includes an MLflow client, not a tracking server. Agents obtain the
central tracking environment from `mlflow.context` or the `start_running`
transition. When `HF_TOKEN` is configured brain-side, Modal's secrets API makes
it available inside the sandbox without returning the value to the agent API.
Never print it or write it into retained files.

## Current agent tool surface

| Tool | Purpose |
|---|---|
| `sandbox.options` | Inspect provider-shaped hardware choices. |
| `sandbox.request` | Reuse or procure a project sandbox, optionally attached to an experiment. |
| `sandbox.get` | Poll or inspect by `sandbox_uid` or experiment attachment. |
| `sandbox.attach` | Attach a running sandbox to another experiment. |
| `sandbox.terminal` | Read transcript output and command status. |
| `sandbox.runs` | Read or long-poll durable `merv_run` receipts. |
| `sandbox.pull_outputs` | Proxy-local rsync of selected compact files into the checkout. |
| `sandbox.extend` | Request a bounded lifetime extension when the provider supports it. |
| `sandbox.release` | Confirm retention, then terminate the machine. |

`sandbox.list` and `sandbox.health` remain available to HTTP/internal callers but
are hidden from agent `tools/list`.

## Review trust boundary

Sandbox transcripts and run receipts provide execution visibility; they do not
prove reviewer independence. Review trust comes from a role- and snapshot-scoped
capability, a distinct `caller_session_id`, and stale/superseded submission
checks. The reviewer skill imposes the read-only operating role; unrelated tool
calls are not authenticated as belonging to that reviewer.
