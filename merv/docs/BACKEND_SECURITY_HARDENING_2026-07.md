# Backend Security Hardening — 2026-07

## Status and intent

This record accompanies `codex/backend-security-hardening`, based on commit
`c61cba0`. The branch is an unreleased, non-production checkpoint for eight
backend security and integrity findings. It must not be treated as deployment
approval: trusted VM host-key enrollment, a storage cutover, and the rollout
checks below remain outstanding.

The implementation keeps policy behind narrow ports and existing service
boundaries. `BlobQuotaAdmission` is the only new cross-domain port; the existing
`QuotaAdmission` and `ObjectStore` ports are extended for atomic reservations
and upload abort. Provider-specific storage and compute logic remains in the
corresponding adapters.

## Findings and intended outcomes

### P0 — Hosted control failed open without authentication

Risk: a hosted brain could start without a Supabase verifier and process calls
as the implicit local principal.

Change:

- `build_control_server` now refuses startup unless `SUPABASE_URL` and
  `SUPABASE_JWT_SECRET` construct a verifier.
- FastAPI assembly independently rejects a hosted-control surface without a
  verifier, preventing alternate composition from reopening it.
- Hosted API, MCP, data-plane, and admin requests pass through bearer
  verification. Health, metadata, preflight, bearer-exempt `/api/sdk/auth/*`
  device-flow bootstrap, and the bearer-exempt but self-verifying MLflow gate
  are intentional exceptions.
- Authenticated project creation records the principal's user membership and
  tenant identity; project reads and mutations enforce membership.

Limits and rollout notes:

- Existing projects need `project_members` backfill before their users can see
  them.
- URL/secret presence is checked at startup; live Supabase reachability and
  credential correctness are not.
- `SUPABASE_SERVICE_KEY` is still required for `rr_sk_` API-key lookup.
- Operator/admin routes are authenticated but have no separate admin role. The
  brain remains a private operator service, not a complete public SaaS boundary.
- The default identity mapping still uses the shared `local` tenant; user
  isolation is currently project-membership based.

### P1 — REST project authorization was bypassable through body fields

Risk: affected handlers merged `{path identifiers, **body}`, allowing an
attacker-controlled body to replace the project or resource authorized by the
URL.

Change:

- Body fields are now merged first and authoritative path identifiers last.
- `project.update`, `claim.create`, `claim.update`, `experiment.transition`,
  and `review.request` enter the principal-aware tool-call funnel.
- `project_id`, `claim_id`, and `experiment_id` from the route cannot be
  replaced by JSON body values.

Future REST mutations must preserve the same body-first/path-last rule; this is
explicit in each handler rather than enforced by a universal router primitive.

### P1 — Debug detail and clear operations were globally scoped

Risk: authenticated users could read global activity/tool-call data, inspect a
different project's call details, and clear the global diagnostic ring.

Change:

- Hosted activity and debug routes require an explicit `project_id` and verify
  membership.
- Stats, detail, and clear receive an allowed project set; cross-project detail
  returns 404.
- Hosted activity excludes unattributed/global events instead of leaking them
  into a project view.
- Local mode intentionally keeps single-user global diagnostics.

Unattributed tool calls are invisible to hosted scoped diagnostics and cannot
be removed through the hosted scoped-clear endpoint.

### P1 — Failed provisioning could hide a running, billing VM

Risk: best-effort cleanup, asynchronous termination, provider lookup outages,
and stale workers could move a row to a terminal state while a provider VM
continued running. A ledger-write failure could also leave an unaccounted VM.

Change:

- A durable `provision_claim` is acquired before provider work. Provisioning
  updates, cancellation, cleanup, and terminal transitions compare claims so a
  stale generation loses write authority.
- The provider sandbox ID is persisted as soon as creation reports it.
- Cleanup takes an exclusive claim, requests termination, and then requires an
  authoritative liveness result. Uncertainty leaves the row in
  `provisioning/cleanup` for retry rather than hiding it as failed/terminated.
- Lambda/Thunder treat only explicit terminal states as gone; provider lookup
  outages no longer collapse to absence. Modal only maps provider NotFound to
  absence.
- Running state and the generation ledger commit atomically. A ledger or actual
  quota revalidation failure enters provider cleanup.
- Release cancellation is scoped to the matching sandbox generation/claim and
  cannot cancel a replacement job.

Residual assumption: this substantially hardens the finding but does not close
every late-create window across multiple replicas and eventually consistent
provider listings. A remote replica's live job has no persisted heartbeat. Once
the stale deadline passes, another replica can take cleanup ownership; an empty
provider lookup before the original create becomes visible can allow the row to
settle before that create returns. Closing that fully requires a persisted
provisioning lease/heartbeat or provider-specific delayed double-negative
confirmation—a larger lifecycle change not included here.

### P1 — Compute and storage quotas were not authoritative

Compute change:

- Admission and the provisioning reservation share one database transaction,
  preventing concurrent requests from passing the same capacity check.
- Active provisioning/running commitments and the new request's full reserved
  lifetime are included in projected USD and GPU-hour budgets.
- Required but unknown price, GPU count, or duration fails closed.
- GPU count is persisted on sandbox and generation rows; multi-GPU runtime is
  charged correctly for new generations.
- Provider-reported price/GPU count is revalidated before the atomic running +
  generation transition.
- Lifetime extension admission and expiry mutation are atomic.

Storage change:

- `StorageLedgerService` calls the narrow `BlobQuotaAdmission` port inside its
  ledger transaction.
- Every pending upload reserves declared bytes; available content is charged
  once per project/SHA to match project-scoped physical namespaces.
- Concurrent reservations serialize through the state store.
- Provider abort/delete must succeed before a ledger status releases quota.
  Physical deletion failure therefore remains visible and retryable instead of
  creating free untracked usage.

Qualifications:

- Compute authority covers concurrency, lifetime, price, GPU-hours, USD, and
  kill switches—not CPU-hours or RAM-hours.
- All replicas must use the identical Postgres DSN so they share the same
  advisory-lock key. Live Postgres contention has not been integration-tested.
- Historical GPU rows are migrated as one GPU for any nonempty GPU label;
  historical multi-GPU usage needs an operator backfill before budget reliance.
- Post-acquire fact revalidation can incur a brief provider charge before a
  disallowed VM is terminated.
- Storage quota covers operations through `StorageLedgerService`, not
  out-of-band bucket writes or provider orphans.

### P1 — Multipart uploads could corrupt content-addressed storage

Risk: multipart data was written directly to its final SHA-derived key and only
size-checked. Incorrect bytes could occupy or overwrite a content-addressed key.

Change:

- Every new S3-compatible upload targets an upload-specific quarantine key.
- Presigned single and multipart requests bind exact `Content-Length` values.
- Completion requires exact total size. Single-part uploads verify provider
  SHA-256; multipart uploads are streamed and rehashed by the brain.
- Only verified bytes are promoted to the final CAS key. Invalid uploads cannot
  modify final content-addressed data and are consumed when provider cleanup
  succeeds; cleanup failure retains the ledger row/quota for retry.
- Explicit upload abort and one-hour pending/completing expiry allow staged
  provider data to be reclaimed.
- Transient hash-read or promotion failures preserve staged data and sidecar
  state for retry; completed multipart assembly is detected so retry does not
  attempt to complete the provider upload twice.

Operational cost: multipart completion reads the staged object to hash it and
streams it again to promote it. Capacity planning must include brain bandwidth,
completion latency, and provider request cost.

### P1 — Server-side fetches were vulnerable to DNS-rebinding SSRF

Risk: the service validated one DNS answer, then `urllib` resolved the hostname
again while connecting.

Change:

- Resolve once, require every answer to be globally routable, and connect the
  socket to a validated address while retaining the original HTTP Host and TLS
  hostname/SNI.
- Re-resolve, validate, and pin every redirect target.
- Reject credentials in URLs, non-web ports, private/loopback/link-local/CGNAT
  and reserved ranges, multicast/unspecified addresses, IPv4-mapped IPv6,
  6to4, and Teredo.

This closes the DNS-rebinding/internal-address route, not every risk of fetching
attacker-controlled public content. The hostname allowlist remains optional.
The direct `http.client` path also does not inherit `HTTP_PROXY`/`HTTPS_PROXY`
behavior, and HTTPS pinning lacks a dedicated TLS regression test.

### P1 — Management SSH disabled host authentication while sending secrets

Risk: `StrictHostKeyChecking=no` plus `/dev/null` allowed a MITM endpoint to
receive HF/MLflow credentials and falsify transcript, metric, or run data.

Change:

- Management SSH now requires `StrictHostKeyChecking=yes` and uses
  `RESEARCH_PLUGIN_MGMT_KNOWN_HOSTS_FILE`.
- Failed secret writes are not marked delivered; later sandbox polls retry.
- Modal uses its authenticated provider exec/secret channel and does not depend
  on management SSH for these operations.

This fix is management-channel scoped. Caller-owned terminal and rsync/output
SSH paths remain data-plane behavior and are outside this host-key change.

Deployment blocker: trusted dynamic host-key enrollment is not implemented.
The reference Compose file passes a path but does not mount, create, populate,
or validate it. Thunder bootstrap fails closed before setup without a trusted
entry. Lambda can reach running after a TCP probe, but transcript/metrics/run
reads and post-boot secret delivery then fail closed. Before enabling either VM
provider, add provider-attested enrollment, a persistent container mount,
endpoint rotation handling, and entry removal. Do not substitute an empty file,
unverified `ssh-keyscan`, `accept-new`, or a shared image-baked host key.

## Schema and compatibility

Migrations 18 and 19 add/backfill generation `gpu_count` and add sandbox
reservation fields (`gpu_count`, `price_known`, and `provision_claim`). The
migration is additive, but the historical multi-GPU limitation above applies.

Most changes are brain-side. Auth-capable `0.0012` and `0.0013` clients remain
wire-compatible. Version `0.0011` predates hosted authentication and must
upgrade or be fenced before rollout. The new plugin-facing schema change is an
optional `sandbox_uid` on `sandbox.request` plus the regenerated tool catalog;
an older auth-capable catalog can still make ordinary requests but cannot expose
intentional UID-based idempotent retry. Storage request/response shape is
unchanged.

The branch still reports package version `0.0013` and
`MIN_PROXY_VERSION = 0.0011`. Before a release, assign a new package/plugin
version and raise the hosted proxy floor to `0.0012`. Do not publish until the
released plugin artifacts are checked against that floor.

## Required storage cutover

Pre-change upload intents are incompatible with the quarantine key layout:
single and multipart provider state points at the old final CAS key, while new
completion/abort targets the upload-specific key. Existing pending rows also
have `expires_at = NULL`, so the new sweeper will not select them even though
quota accounting continues to charge them.

Before deploying this branch with heavy storage enabled, either add a versioned
compatibility migration or perform a drain:

1. Stop new heavy-object upload registration.
2. Under the old code, complete or cancel every `uploading`/`completing` row.
3. Inspect and abort residual provider multipart uploads and upload sidecars.
4. Require this query to return no rows before cutover:

   ```sql
   SELECT status, COUNT(*), COALESCE(SUM(size_bytes), 0)
   FROM storage_objects
   WHERE status IN ('uploading', 'completing')
   GROUP BY status;
   ```

5. Deploy, then run a real MinIO/S3 single- and multipart round-trip before
   accepting uploads.

## Verification and evidence limits

Verification captured on 2026-07-13:

- Consolidated security regression suite: `450 passed, 8 skipped`.
- Repository suite excluding one known baseline failure:
  `1213 passed, 19 skipped, 1 deselected`.
- The deselected live-Postgres legacy sandbox-identity migration test fails
  identically at base commit `c61cba0`; this branch neither caused nor modifies
  that migration-order defect.
- Docker-simulated VM bootstrap with a host key enrolled through the trusted
  test-fixture control channel: `2 passed`.
- Python compileall and `git diff --check`: passed.
- Compose without Supabase verifier values: rejected as required; configured
  Compose validation: passed.

Coverage includes mandatory hosted auth, cross-project body-ID attacks, scoped
debug clear/detail, pinned-address SSRF, cleanup ownership and liveness
uncertainty, quota reservation races, multi-GPU accounting, storage
quota/refcount failures, transient S3 completion retry, quarantine integrity,
and strict management SSH.

The test suite does not replace live verification of Supabase credentials,
provider eventual consistency, multi-replica Postgres contention, dynamic host
key enrollment, or real S3-compatible multipart behavior. Those remain explicit
pre-deployment gates.
