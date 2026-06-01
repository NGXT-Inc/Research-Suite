# Review Identity

## Problem

Design and experiment review must be independent enough to matter. In a local
Codex workflow, identity cannot rely on IP address, user account, or machine
boundary. Checking an agent's memory is also not a reliable enforcement
mechanism.

## Principle

Treat reviewer identity as workflow identity:

```text
review request + scoped capability + reviewer session + immutable target snapshot
```

The MCP server should enforce the review gate with capability-scoped sessions,
not with trust in prompt text.

## Minimum viable protocol

1. Main agent creates or updates an experiment plan/result through MCP.
2. MCP records the producer session and target snapshot.
3. Main agent asks MCP for a review request:

   ```text
   review.request(project_id, target_type, target_id, role, reason)
   ```

   The `project_id` is explicit. MCP must not infer the active project from prior
   state.

4. MCP returns a one-time reviewer capability:

   ```json
   {
     "review_request_id": "rr_...",
     "role": "design_reviewer",
     "target_snapshot_id": "snap_...",
     "reviewer_capability": "opaque-one-time-token",
     "read_scope": ["claim", "experiment", "resources"],
     "expires_at": "2026-05-17T15:00:00Z"
   }
   ```

5. Main agent spawns a separate reviewer agent with the role skill and passes the
   capability.
6. Reviewer starts a review session:

   ```text
   review.start(review_request_id, reviewer_capability, declared_agent)
   ```

7. MCP gives that session read-only access to the target scope.
8. Reviewer submits:

   ```text
   review.submit(review_session_id, verdict, notes, findings, evidence?)
   ```

## Enforced checks

MCP rejects review submission when:

- capability is expired, reused, or unknown
- role does not match the active gate
- target snapshot changed after the capability was issued
- reviewer session equals the producer session
- reviewer tries to call mutation tools
- review does not cite required target context

## Important limitation

Without trusted per-agent call metadata from the Codex client, this is not
cryptographic proof of independence. A malicious or careless main agent could
use the review capability itself.

The MVP should therefore distinguish:

- `verified_agent_review`: MCP received trusted distinct agent/session metadata
  from the client.
- `attested_agent_review`: MCP enforced capability separation, but distinct
  local agent identity is self-declared.
- `human_review`: human decision recorded through the same review mechanism.

High-risk gates can require `verified_agent_review` or `human_review`.

## Better future option

If Codex can provide unforgeable MCP call metadata, the server should compare:

```text
producer_agent_session_id != reviewer_agent_session_id
```

and store:

- producer session id
- reviewer session id
- parent/child relationship if available
- skill name used by reviewer
- target snapshot id
- review request id
- review transcript hash or summary

That would make reviewer independence a client-enforced identity property rather
than a convention.
