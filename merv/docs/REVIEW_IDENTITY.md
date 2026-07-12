# Review Identity

## Boundary

Design, experiment, and reflection reviews are intended to come from a session
distinct from the producer. The brain enforces a declared-session separation:

```text
review request + request-scoped capability + reviewer session + immutable target snapshot
```

The mechanism does not trust prompt text, IP addresses, or a claimed agent name.
It also cannot authenticate the caller-supplied session strings, so the boundary
is workflow-level rather than cryptographic identity.

## Protocol

1. The producer submits the plan, attempt artifacts, or reflection artifacts.
2. The producer calls:

   ```text
   review.request(target_type, target_id, role, reason?, producer_session_id?)
   ```

3. For `design_reviewer`, `experiment_reviewer`, and `reflection_reviewer`, the
   brain validates that the role matches the active gate. `human` and
   `automated_check` are gate-exempt. The brain pins the target snapshot, hashes
   a newly minted capability, and stores only that hash.
4. The response returns the plaintext capability once, together with
   `reviewer_handoff.spawn_prompt` containing the correct reviewer skill and
   target context.
5. A separate reviewer session presents the capability:

   ```text
   review.start(review_request_id, reviewer_capability, caller_session_id, declared_agent?)
   ```

   `caller_session_id` is required and its declared value must differ from the
   producer's declared session value.
6. `review.start` returns the current attempt's submitted gated-role artifacts
   plus any system metrics exhibit. Ordinary code, input, result, config, model,
   and note resources are not bundled; reviewers obtain any additional context
   through ordinary read-only calls. The reviewer skill imposes a procedural
   read-only role. The reviewer submits one structured verdict through:

   ```text
   review.submit(review_session_id, verdict, synopsis, return_to?, notes?, findings?, evidence?)
   ```

The requesting session must not start the review on the reviewer's behalf. The
server can compare the two declared strings, but cannot prove which client made
the call.

## Snapshot and capability checks

At `review.start`, the brain rejects the call when:

- the capability is missing, expired, or invalid;
- a newer request superseded the request;
- the target snapshot changed after the request was created; or
- the declared `caller_session_id` equals the declared producer session.

The workflow-role/gate match is checked earlier, when `review.request` creates
the request.

At `review.submit`, the caller presents only `review_session_id`. The brain
rejects a missing/already-submitted session, a request that is no longer open, a
changed target snapshot, or a payload/`return_to` that violates the role
contract. It does not receive or recheck the capability, its expiry, or the
caller session at submission time.

The request remains startable while its status is `requested` or `started` and
its capability is unexpired, so a capability is not consumed by the first
`review.start`. The first accepted submission closes the request, so any other
started session can no longer submit. The reviewer skills provide the read-only
operating boundary; the hard server boundary is that only a passing
`review.submit` with the workflow gate's exact role and current snapshot can
satisfy that gate. The dispatcher also rejects other mutations when they
explicitly carry a `review_session_id`.

Requesting a fresh capability for the same target and role is
revoke-and-reissue: all prior requested or started sessions for that gate become
`superseded`. Plaintext capabilities are never recoverable from durable state.

## What the snapshot pins

The snapshot identifies the target status, attempt, and exact submitted resource
versions. `review.start` bundles pinned bytes for the gated artifacts and any
system metrics exhibit; ordinary resource versions remain snapshot references
but their bytes are not included in that response. Reviewers judge the bundled
submissions rather than later working-tree edits. A gated file revision must be
re-registered and reviewed under a fresh snapshot.

Experiment-attempt rejections must choose:

- `return_to="running"` when the approved plan still stands and execution or the
  conclusion needs work;
- `return_to="planned"` when the design is flawed and a new attempt is required.

Reflection rejections choose `synthesizing` or `reflecting`. Design-review
rejections always return to `planned`.

## Independence level

Every newly created reviewer session records `verified_agent_review` because a
distinct, non-empty `caller_session_id` is mandatory. `attested_agent_review`
exists only on legacy rows created before that requirement.

This records only that two caller-supplied strings were non-empty and unequal.
It does not prove that two clients—or two independent models—performed the
reasoning, and possession of `review_session_id` is sufficient to submit. The
current clients do not provide unforgeable per-agent identity metadata.
