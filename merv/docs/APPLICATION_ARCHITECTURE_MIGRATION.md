# Application Architecture Migration

Status: implemented and verified. Independent adversarial reviews returned
**GO** for both the revised plan and the final implementation.

## Decision and scope

Adopt these classifications as architectural facts:

- `research_core`, `artifacts`, `sandbox`, and `feed` are business/domain
  components; each may contain domain policy, application services, ports, and
  adapters internally.
- `mlflow` is an outbound experiment-tracking adapter.
- concrete code under `object_storage` is outbound persistence infrastructure.
  The current `object_storage/service.py` is a mixed-in application/ledger
  service and must be classified inward rather than mislabeled as an adapter
  merely because of its directory; physical relocation is deferred.
- a new `application` module owns cross-domain use cases and outbound ports.
- `surface` is delivery code (HTTP/MCP/auth/serialization) plus a temporarily
  co-located composition root. Composition is an outer/bootstrap role, not a
  business module.
- `kernel` remains the dependency floor.

This migration establishes the boundary across experiment transitions,
tracking finalization/context, review reactions, composite UI queries,
resource-content presentation, operator maintenance, permission vocabulary,
and merged tool operations. It preserves all public routes, tool schemas,
responses, event rows, transaction cuts, and best-effort failure behavior.
The only intentional behavior change is redacting the already-returned MLflow
password from internal telemetry; public responses remain unchanged.
It does not turn the modular monolith into distributed services, add a second
event stream, make reactions asynchronous, or auto-publish feed posts.

The earlier LOC-reduction session's local/remote plane split remains
inviolable. This is a separate architecture branch, so its prohibition on
changing module edges does not apply; public behavior, the proxy catalog, and
the server/local split still do.

## Evidence behind the change

Today `surface/tools/tool_handlers.py::experiment_transition_agent` composes
four authorities:

1. metrics-exhibit generation and pinning;
2. the ResearchCore workflow transition;
3. MLflow run creation/finalization and research-state refresh;
4. a terminal feed advisory.

Both REST and MCP already converge on this handler through `ControlApp` and
`ToolDispatcher`. `ExperimentService.transition` atomically changes experiment
state and appends `experiment.transitioned`. Surface is therefore large not
because the domain decomposition makes a thin edge impossible, but because no
application/use-case layer currently has permission to coordinate the modules.

The event ledger is not disposable logging. Existing rows drive SSE cursors,
ETags, attempt-window derivation, feed cadence, claim history, and project
orientation. Event names, payloads, counts, and order are compatibility
contracts.

## Target dependency law

```text
                       bootstrap/composition
                    /      |       |       \
                   v       v       v        v
             delivery -> application <- adapters
                              |
                              v
                        component facades
                              |
                              v
                            kernel
```

The executable checker will classify every brain file twice, using
deepest-prefix-wins tables plus file overrides. Unknown files fail. Component
answers “which capability owns this?”; layer answers “what architectural job
does this file perform?”

| Path | Component |
|---|---|
| `kernel/**` | Kernel |
| `research_core/**` | Research |
| `artifacts/**` | Artifacts |
| `sandbox/**` | Sandbox |
| `feed/**` | Feed |
| `mlflow/**` | Tracking integration |
| `object_storage/**` | Storage |
| `application/**` | Cross-component application |
| `surface/**` | Surface |

The initial layer table is explicit about mixed packages:

| Path (deepest match wins) | Layer |
|---|---|
| `kernel/**` | foundation |
| `kernel/ports/**` | port |
| `kernel/state/dialects.py` | adapter |
| `research_core/**` | application |
| `research_core/domain/**` | domain |
| `artifacts/**` | application |
| `artifacts/{figure_view,resource_selection}.py` | domain |
| `feed/**` | application |
| `feed/feed_policy.py` | domain |
| `feed/feed_unfurl.py` | adapter |
| `sandbox/**` | application |
| `sandbox/sandbox_backend.py` | port |
| `sandbox/execution/backends/**`, `sandbox/execution/{multiplexer,vm_ssh}.py`, `sandbox/{managed_mgmt_keys,mgmt_keys,ssh_keys}.py` | adapter |
| `sandbox/execution/{__init__,driver_registry}.py` | bootstrap |
| `mlflow/**` | adapter |
| `object_storage/{blobs,s3_blobs,s3_object_store}.py` | adapter |
| `object_storage/service.py` | application |
| `application/**` | application |
| `application/ports/**` | port |
| `surface/**` | delivery |
| `surface/composition/**`, `surface/config.py`, `surface/control/{control_app,record_core}.py` | bootstrap |
| `surface/control/{control_client,control_runtime}.py` | adapter |
| `surface/tools/tool_handlers.py` | delivery |

`Sandbox`, `Feed`, and the other names above are **components**, not claims
that every file in their current package is domain-layer code. In particular,
the recently formalized sandbox provider drivers are adapter-layer code inside
the Sandbox component.

`kernel/state/store.py` remains a pragmatic shared persistence floor: it
contains the base store contract and the local SQLite implementation together.
This slice does not pretend that seam is cleaner than it is or split it merely
for taxonomy; the separate Postgres dialect is classified as an adapter.

The independent layer law is:

- foundation -> foundation;
- port -> port/foundation;
- domain -> domain/port/foundation;
- application -> application/domain/port/foundation;
- adapter -> adapter/application/domain/port/foundation (inward only);
- delivery -> delivery/application/port/foundation;
- bootstrap -> any layer;
- nothing except bootstrap imports bootstrap; no non-delivery layer imports
  delivery.

The component import matrix is exact (row imports columns):

| Importer | Allowed components |
|---|---|
| Kernel | Kernel |
| Research | Research, Artifacts, Kernel |
| Artifacts | Artifacts, Kernel |
| Sandbox | Sandbox, Kernel |
| Feed | Feed, Kernel |
| Cross-component Application | Application, Research, Artifacts, Feed, Kernel |
| Tracking integration | Tracking, Application, Kernel |
| Storage | Storage, Kernel |
| Surface | any component (the independent layer law constrains delivery files) |

SQL ownership remains component-based: moving Storage ledger policy to the
application layer does not make `storage_objects` an Application-component
table. The unused Feed -> Storage and MLflow -> Research allowances are
removed.

Two exact-pair exception sets make the transition honest and monotonic:

- `LAYER_EXCEPTIONS` freezes the remaining delivery/bootstrap deep imports.
  The Surface owners are `surface/{auth,identity,observability}.py` and
  `surface/tools/contracts.py`. The implementation records exact
  importer/target file pairs, not wildcard exemptions; fixed pairs must be
  deleted and new pairs fail.
- `feed/feed.py -> feed/feed_unfurl.py` is a named application-to-network-adapter
  exception. A later Feed use case will introduce and inject `LinkUnfurlPort`.

This slice must remove every exception attributable to experiment-transition
or exhibit orchestration. A separate structure assertion allows
`application/**` to import another component only through its declared
`facade.py` or `ports/**` entrypoint. Existing non-migrated Surface work remains
visible in the exception ledger rather than being hidden behind fake wrappers.

## Ports based on current use

### Experiment tracking

The transition slice needs only this application-owned structural contract:

```python
@dataclass(frozen=True)
class TrackingCapabilities:
    logging: bool
    control: bool
    readback: bool

class TrackingContextPayload(TypedDict, total=False):
    configured: bool
    mode: str
    tracking_uri: str
    dashboard_url: str
    experiment_name: str
    env: dict[str, str]
    note: str

class TrackingContext(Protocol):
    def to_dict(self) -> TrackingContextPayload: ...

class TrackingRun(TypedDict, total=False):
    run_id: str
    run_name: str
    status: str
    artifact_uri: str
    created_at: str
    created_by_plugin: bool
    error: str

class CreateRunResult(TypedDict, total=False):
    created: bool
    run_id: str
    run_name: str
    status: str
    artifact_uri: str
    created_at: str
    created_by_plugin: bool
    error: str

class FinalizeRunResult(TypedDict, total=False):
    run: TrackingRun

class TrackingMetric(TypedDict, total=False):
    last: float | None
    step: object
    min: float
    max: float

class TrackingSnapshotRun(TypedDict, total=False):
    run_id: str
    run_name: str
    status: str
    start_time: int
    end_time: int
    params: dict[str, object]
    tags: dict[str, str]
    metrics: dict[str, TrackingMetric]
    metrics_capped_at: int

class TrackingExperimentSnapshot(TypedDict, total=False):
    name: str
    runs: list[TrackingSnapshotRun]

class MetricsSnapshot(TypedDict, total=False):
    available: bool
    experiments: list[TrackingExperimentSnapshot]

class ExperimentTracking(Protocol):
    def capabilities(self) -> TrackingCapabilities: ...

    def context(
        self, *, project_id: str, experiment_id: str,
        include_credentials: bool = False,
    ) -> TrackingContext: ...

    def create_run(
        self, *, project_id: str, experiment_id: str,
        attempt_index: int, run_name: str,
    ) -> CreateRunResult: ...

    def finalize_run(
        self, *, project_id: str, experiment_id: str,
        run_id: str, status: str, wait_seconds: float,
    ) -> FinalizeRunResult: ...

    def results_metrics(
        self, *, project_id: str, experiment_id: str,
    ) -> MetricsSnapshot: ...
```

`TrackingContext` promises only `to_dict() -> TrackingContextPayload`; the
application does not inspect adapter object attributes. Command and snapshot
DTOs likewise promise only fields the application actually reads. MLflow may
return richer dictionaries to preserve its existing direct Surface responses,
but those extra fields are adapter output, not requirements placed on every
tracking implementation. These types are internal contracts, not new wire
models. `TrackingCapabilities` distinguishes
agent logging, control-plane mutation, and readback configuration. That
distinction preserves the current server-only/read-only modes that a single
`configured` boolean would accidentally collapse. `CentralMlflowService`
implements this port; the application must not read its raw `tracking_uri` or
`server_uri` fields.

Capability truth table (and required contract test):

| Configuration | logging | control | readback | Current behavior retained |
|---|---:|---:|---:|---|
| neither URI | false | false | false | context unconfigured; no exhibit query |
| public tracking URI only | true | false | true | agents can log/read; plugin cannot create/update |
| server URI only | false | true | true | no agent logging env; control/readback available |
| both URIs | true | true | true | full creation, update, and readback |

Project-wide MLflow browsing, health, and namespace discovery do not belong in
this command port. The follow-on Surface migration therefore defines a separate
narrow `TrackingOverview` query protocol in the application query module.

Pure exhibit construction and visibility/naming policy move from the MLflow
adapter into application-owned code. MLflow keeps compatibility re-exports so
existing callers do not break; tests assert re-exported symbol identity so
existing monkeypatch targets keep working. REST snapshot mechanics and MLflow
HTTP remain inside the adapter.

### Storage

There are two distinct storage seams and they must not be conflated:

1. Submitted evidence bytes used by Artifacts and Feed. Move `BlobStat` and the
   narrow `EvidenceBlobStore` (`put`, `get`, `stat`) to `kernel/ports`. Define a
   separate `ExpiringBlobStore.sweep_expired` cleanup port. Presigning,
   deletion, and finalize remain an optional adapter-transfer extension because
   no production domain caller currently uses them. Adapter implementations
   remain in `object_storage` and retain identity-preserving compatibility
   exports. Namespace/SHA validation moves beside the port in kernel so the
   application-layer storage ledger no longer imports a private adapter helper.
2. Heavy objects. Keep the existing `kernel.ports.object_store.ObjectStore`
   (`presign_upload`, `complete_upload`, `presign_download`, `stat`, `delete`),
   because it already matches `StorageLedgerService` usage. Add TypedDict
   `UploadTarget`/`DownloadTarget` results in place of undocumented dictionaries
   without changing their wire shapes.

`StorageLedgerService` itself is not a provider adapter: it owns versioning,
TTL, deduplication, lifecycle events, concurrency, and reference-aware
reclamation. Classify that file as Storage-component/application-layer code in
place for this slice; do not perform an unrelated 800-line relocation. A later
storage use-case extraction can move it behind a stable storage facade with an
identity-preserving old-path export. Local/S3 byte movement is classified as
the adapter now. This is the concrete architectural change implied by calling
“object storage” infrastructure rather than a peer domain without pretending
that the current ledger policy is infrastructure.

Replace `ExperimentService.storage_objects_reader: Any` with a named,
research-owned query protocol for the existing transaction-aware
`objects_for_experiment(conn, project_id, experiment_id)` seam. Changing that
callback to open an independent connection would alter snapshot and performance
semantics, so removing the `conn` parameter is explicitly deferred.

## Stable component facades

Facades are introduced only after the use-case and event seams are stable.
For this slice they are structural Protocols over typed slice DTOs, not concrete
implementation exports:

```python
class PersistedRunState(TypedDict, total=False):
    run_id: str | None
    run_name: str
    status: str
    artifact_uri: str
    created_at: str | None
    created_by_plugin: bool
    error: str

class ExperimentState(TypedDict, total=False):
    id: str
    project_id: str
    name: str
    status: str
    attempt_index: int
    mlflow_run: PersistedRunState | None

class MetricFileSource(TypedDict):
    path: str
    version_id: str
    sha256: str
    observed_at: str
    data: object

class ExhibitVerdict(TypedDict, total=False):
    runs_found: int
    result_files: int
    attempt_index: int
    mlflow: dict[str, object]
    pinned: bool

class SlimExperimentState(TypedDict, total=False):
    id: str
    project_id: str
    name: str
    status: str
    attempt_index: int
    mlflow_run: PersistedRunState | None

@dataclass(frozen=True)
class CommittedExperimentTransition:
    state: ExperimentState
    event: StoredEvent

class ResearchCore(Protocol):
    def experiment_state(
        self, *, experiment_id: str, project_id: str | None = None,
    ) -> ExperimentState: ...
    def transition_experiment(
        self, *, experiment_id: str, transition: str,
        evidence: dict[str, object] | None = None,
        project_id: str | None = None,
    ) -> CommittedExperimentTransition: ...
    def record_tracking_run(
        self, *, project_id: str, experiment_id: str,
        run: PersistedRunState,
        event_type: str | None = None,
    ) -> ExperimentState: ...
    def record_exhibit_verdict(
        self, *, experiment_id: str, project_id: str,
        verdict: ExhibitVerdict,
    ) -> None: ...
    def attempt_started_running_at(self, *, experiment_id: str) -> str | None: ...
    def present_experiment(self, state: ExperimentState) -> SlimExperimentState: ...

class Artifacts(Protocol):
    def metric_file_sources(
        self, *, experiment_id: str, attempt_index: int,
    ) -> list[MetricFileSource]: ...
    def pin_system_artifact(
        self, *, path: str, experiment_id: str, role: str,
        content_bytes: bytes, content_type: str, title: str,
        kind: str, project_id: str,
    ) -> None: ...

class Feed(Protocol):
    def transition_advisory(
        self, *, project_id: str, experiment_id: str, event: str,
    ) -> str | None: ...
```

`PersistedRunState`, `ExperimentState`, `SlimExperimentState`,
`ExhibitVerdict`, and `CommittedExperimentTransition` are Research-owned types;
`MetricFileSource` is Artifacts-owned. They import only Kernel event types.
Application owns the tracking-port DTOs and a `TransitionResponse` that combines
the Research slim state with `TrackingContextPayload`, exhibit, and feed fields.
The tracking reaction explicitly translates `CreateRunResult`/`TrackingRun`
into `PersistedRunState` before calling Research. Research never imports the
Application component.

```python
class TransitionResponse(SlimExperimentState, total=False):
    mlflow: TrackingContextPayload
    mlflow_guidance: str
    metrics_exhibit: dict[str, object]
    feed_note: str
```

Concrete facades delegate to the already-composed services and normalize only
the slice's types/names. A fake-backed facade contract suite runs against both
recording fakes and the real composed facades. Composition identity tests prove
that the facades reference the exact existing `ExperimentService`,
`ResourceService`, and `FeedService` instances rather than constructing shadow
services/stores.

Sandbox is a component in the classification, but this slice does **not**
canonize the 1,431-line `SandboxService` as a stable facade. Its future facade
will be derived from a narrow sandbox use case (likely separate catalog,
lifecycle, and management facets) and is explicitly deferred. The current
formal driver port remains the provider boundary, not the component facade.

Application code imports explicit stable entrypoints such as
`research_core.facade`, `artifacts.facade`, and `feed.facade`, never internal
service files. Package roots may re-export those facades only when doing so does
not eagerly load optional adapters. The facade may delegate internally today,
but callers do not receive or type against the concrete internal services.
Broader facade methods are added only when another use case migrates; a giant
pass-through object mirroring every service method is a non-goal.

Bootstrap may keep legacy `ControlApp.experiments`, `.resources`, `.feed`, and
`.sandboxes` aliases temporarily because tests and non-migrated delivery code
use them. They point at the same instances and are not the new application
dependency surface.

## Committed event and dispatch semantics

`BaseStateStore.record_event` will return an immutable `StoredEvent` while
preserving every existing insert. The implementation computes `created_at` and
canonical `payload_json = json.dumps(payload or {}, sort_keys=True)` exactly
once, inserts those values with dialect-neutral `INSERT ... RETURNING id`, and
builds the result from that ID plus `json.loads(payload_json)`. `MAX(id)`, a
second “last row” query, and SQLite-only `lastrowid` are forbidden. The payload
is recursively defensively copied/frozen (mappings read-only, sequences tuples),
so caller mutation cannot alter the dispatched value. Existing callers may
ignore the return; `events_since` keeps its current mutable JSON wire shape.

```python
@dataclass(frozen=True)
class StoredEvent:
    id: int
    project_id: str
    type: str
    target_type: str
    target_id: str
    payload: FrozenJsonObject
    created_at: str
```

`FrozenJsonObject` recursively contains read-only mappings, tuples, and JSON
scalars only.

`ExperimentService` adds a compatibility-preserving transition variant that
returns both state and the exact `StoredEvent`. The existing `transition()`
continues returning only state. The event becomes observable to application
code only after the service method exits and its transaction commits.

An application `EventDispatcher` has explicit registration by event type,
reaction phase, stable handler name, and `fatal` or `advisory` failure mode. It
dispatches an immutable command
context containing the exact committed event plus the state snapshot threaded
through the command. It is a small synchronous registry/sequencer (maximum 100
production lines), not a bus framework. For this slice:

- it receives the exact committed event returned by the command;
- it never scans or appends to the ledger;
- it never runs from inside `record_event` or a database transaction;
- handlers run in deterministic registration order within an explicitly
  selected phase;
- duplicate handler names and unknown failure modes are rejected;
- an unknown event/phase is a no-op that returns the input state and no
  outcomes;
- dispatch adds no acknowledgement/retry rows to the public ledger;
- handlers return a reaction containing the state to thread to the next handler
  (the original snapshot for a no-op/failure) plus an optional named value;
- the dispatcher returns the final threaded state and named outcomes so the use
  case can assemble the same immediate response as today without a new read.

Fatal failures propagate immediately and stop their phase. Advisory failures
produce no outcome and later handlers continue. Feed registrations are
advisory; start/retry tracking is fatal because failure to persist a normalized
adapter result already surfaced to the caller. Terminal tracking retains its
existing internal best-effort suppression.

The registry stores no delivery state or deduplication. The review Feed handler
is repeat-safe because producer reads can recur. Tracking reactions are not
automatically replayed and are not assumed idempotent across a remote call plus
local persistence. The stable key for any future durable delivery is
`(event.id, phase, handler_name)`. In-memory “already delivered” state is
forbidden: it would break repeated producer reads and disappear on restart.

This is deliberately not a durable asynchronous outbox processor. A crash
after commit can skip these best-effort reactions today and can still do so
after this refactor. Durable replay would require consumer checkpoints,
idempotency keys for external systems, and independent retry policy; it is a
separate feature, not something to imply with an in-memory bus.

The boundary is also not atomic or idempotent across MLflow and the database:
remote run creation may succeed before `record_tracking_run` fails. This slice
does not replay, but any future replay/redispatch design must assume that the
external call can have happened and supply an idempotency strategy before it is
enabled.

Transition commands dispatch only the exact `experiment.transitioned` event
they return. Events appended by `record_tracking_run` are not recursively
dispatched; there is no automatic ledger scanner.

The first handlers subscribe to `experiment.transitioned`:

- `FeedTransitionAdvisory` (post-response phase): maps the final threaded
  state's status to the existing `feed_note_for` call, catches all failures,
  and returns an optional `feed_note`. It does **not** create a post. The
  committed event is the trigger, not the source of the status mapping.
- `TrackingTransitionReaction`: creates/reuses a run after start/retry and
  finalizes plugin-owned runs after submit/complete/abandon/failure. It keeps
  current best-effort semantics and returns the exact state produced by
  `record_mlflow_run`, or the input state when no change occurs.

The feed handler is migrated first as a low-risk batch, but final runtime
phases preserve today's order: tracking reacts; base response, tracking context,
and exhibit fields are assembled; only then is the terminal feed advisory
queried and attached. “Feed first, MLflow next” describes migration order, not
runtime execution order. This timing matters: a context-serialization failure
must prevent the feed call, and a concurrent feed post during response assembly
must still suppress the advisory as it does today.

The second subscription is `review.submitted` in the `producer_read` phase.
`ReadReviewStatus` first performs the canonical status read. Only when an
experiment verdict exists does it resolve the experiment and look up the exact
newest durable `review.submitted` event for that target, then synchronously
dispatch the Feed advisory before returning. This preserves the original
producer-facing timing: the reviewer sees its verdict at submit, while the
producer receives the optional note when reading status. Status failures remain
fatal; project/event correlation and Feed failures remain advisory. Repeated
reads may dispatch the same event ID and return the same read-only advisory
until an actual Feed post suppresses it. No event, acknowledgement, or cursor
row is appended by the read.

The third subscription is `experiment.mlflow_run_refreshed` in the
`post_response` phase. Canonical `mlflow.finalize_run` dispatches the exact event
returned with Research's persisted readback, then attaches the advisory outcome.
The explicit foreign-run compatibility path intentionally remains a direct
best-effort advisory: it cannot dispatch because it writes no event and must not
fabricate one.

`ControlApp` owns the single reaction-registry instance and injects it into
`TransitionExperiment`, `ReadReviewStatus`, and `FinalizeTrackingRun`.
`ExperimentReactions` binds transition tracking, terminal Feed,
tracking-finalization Feed, and review-verdict Feed handlers once during
composition. Use cases no longer construct or register private handler sets.

Exhibit generation is not an event reaction: it must remain a synchronous
prerequisite before `submit_results`, because the workflow gate validates the
pinned exhibit.

## Representative use case

`application/experiments/transition.py::TransitionExperiment.execute` owns this
exact order:

1. For `submit_results` while running, generate the exhibit, record its verdict,
   and optionally pin it. Those existing transactions commit before the gate.
2. Ask `ResearchCore` to transition and return `(state, committed_event)`.
3. Dispatch the committed event's post-commit phase to the tracking handler.
4. Use the state threaded through the reactions; do not reload it. A reload
   could observe a concurrent write and adds a new failure point absent today.
5. Produce the existing slim state, MLflow context/guidance, and exhibit
   expectation or pinned verdict.
6. Dispatch the same committed event's post-response phase with that final
   threaded state, then attach its optional feed advisory and return.

`surface/tools/tool_handlers.py` maps `experiment.transition` directly to
`TransitionExperiment.execute`; no cross-module logic remains in that handler.
HTTP and MCP retain their current contracts and both reach the same use-case
object through the existing dispatcher.

## Credential compatibility decision

The current `experiment.transition` tool asks tracking context for agent
credentials. Both MCP and the REST transition route call that tool, so both
currently receive the credential-bearing environment block; the ordinary UI
`GET /experiments/{id}` view calls context without credentials. This is an
existing audience-boundary concern, but silently fixing it inside an
architecture refactor would change behavior and invalidate route parity.

This slice therefore deliberately preserves and characterizes the existing
behavior:

- MCP transition: credential variables present when configured;
- REST transition: the same credential variables present;
- ordinary UI experiment-state GET: credential variables absent.

One security correction is explicitly authorized before orchestration moves:
current activity/tool-call telemetry recursively records the credential-bearing
result, while `SENSITIVE_KEYS` does not include `MLFLOW_TRACKING_PASSWORD`.
Add that exact key to central recursive redaction first and test all activity,
tool-call, and structured-log sinks. Public MCP/REST responses remain
credential-bearing as above; telemetry changes from plaintext to
`"[redacted]"`. This is the sole intentional behavior change in the migration.

The use case takes an explicit internal `include_tracking_credentials` command
flag; the shared tool-delivery adapter passes `True`, and both current routes
converge there. This makes the decision visible rather than hard-coded in a
helper. A follow-up security change can give REST a separate `False` adapter
(or split an agent command from the browser mutation) with an
intentional public-contract decision. No secret value is logged in event
payloads, dispatcher outcomes, or test snapshots.

## Behavior invariants

- `submit_results` exhibit verdict/pin remains committed even if the subsequent
  workflow gate fails.
- `experiment.transitioned` stays in the same transaction as state/conclusion.
- no handler sees an uncommitted event.
- start/retry run creation happens after transition commit and can record either
  a run or the existing normalized creation error.
- terminal tracking finalization never reverses a committed workflow transition
  and exceptions remain suppressed.
- feed failures never break a transition and completion does not publish a post.
- event names, payloads, counts, and ordering are unchanged.
- HTTP/MCP response shapes and public credential delivery, tool schemas, route
  paths, proxy catalog bytes, DB schema, and local/remote plane split are
  unchanged; internal telemetry gains the explicit password redaction above.

## Implementation batches

Each batch must be independently testable and committed in the isolated
`codex/application-architecture` worktree.

0. **Freeze public behavior and seal credential telemetry before moving code**
   - add characterization tests for the mandatory matrix, especially current
     credential audiences, event sequences, failure residue, state threading,
     and feed timing;
   - add `MLFLOW_TRACKING_PASSWORD` to recursive telemetry redaction and prove
     credential-bearing responses are unchanged while every telemetry sink is
     redacted;
   - record the historical catalog hash and production LOC baseline;
   - make no other production behavior change in this batch.
1. **Classify roles and extract storage ports**
   - implement independent file-exact component/layer classifiers, laws, and
     shrinking exact-pair exception ledgers;
   - move blob protocols/DTO to kernel ports with adapter compatibility exports;
   - type the research storage-object query seam;
   - classify the storage ledger inward while leaving local/S3 implementations
     as adapters; do not relocate the ledger in this slice;
   - prove domains no longer import `object_storage`.
2. **Extract tracking port and pure application policy**
   - create `application/ports/tracking.py`;
   - move pure exhibit/visibility/naming policy inward;
   - adapt `CentralMlflowService` without changing its public behavior;
   - prove application has no MLflow import.
3. **Introduce committed-event dispatch**
   - return `StoredEvent` from ledger writes;
   - add transition-with-event compatibility API;
   - implement explicit synchronous dispatcher and focused semantics tests.
4. **Move the transition use case**
   - introduce narrow ResearchCore/Artifacts/Feed facades;
   - move exhibit prerequisite and response assembly to the application use case;
   - move feed advisory, then post-transition tracking, to registered handlers;
   - reduce Surface's transition handler to delegation.
5. **Prove shared delivery and ratchet the facades**
   - add REST and MCP spy/parity tests against the same use-case instance;
   - enforce application-to-domain-root imports;
   - run fake conformance and real-composition identity tests for the three
     slice facades; retain the documented narrow Sandbox-facade design without
     exporting `SandboxService` as one;
   - document remaining Surface orchestration as the next migration queue.
6. **Verify and compact**
   - remove superseded helpers/imports rather than retaining duplicate paths;
   - require `tool_handlers.py` plus the old Surface exhibit orchestration to
     shrink by at least 120 production lines, compatibility wrappers to contain
     no implementation and stay at 15 lines or fewer each, and total brain
     production growth to stay at or below 300 lines; exceeding a bound requires
     a new adversarial ratification rather than an explanation after the fact;
   - run all gates below.

### LOC gate re-ratification

The original absolute ceiling was 40,224 brain lines (the 39,924 baseline plus
300). Implementation showed that ceiling contradicted the already-ratified
seams: the tracking port (227 lines), three typed facades (286), and explicit
stored-event/dispatcher/committed-transition values (152) alone add 665 gross
lines before storage ports or persistence plumbing.

An independent compactness audit identified 106 architecture-preserving lines
and rejected comment removal, formatting compression, facade weakening, and
test deletion. After those reductions, a second independent adversarial review
re-ratified an absolute ceiling of **40,860** (+936) for the initial migration.
The tracking follow-up then added an explicit typed command/query boundary and
exact refresh-event return. Its isolated branch temporarily raised the ceiling
to 41,000, but the combined integration did not accept that re-ratification.
Instead, a whole-tree reachability audit removed an equal amount of stranded
backfill, reflection-lint, request-helper, HTTP-helper, activity-producer, and
contract-projection code whose callers or routes had already been retired.

After integrating tracking, the shared reaction registry, and the final
Surface-ownership slices, the implemented tree is **40,848 brain lines**, two
below that slice's fixed 40,850 ceiling and three above the branch starting point. The MLflow compatibility wrapper remains 13
lines, `tool_handlers.py` is 94 lines (down from 1,022 before the migration),
and `views.py` is 452 lines (down from 763 before the final extraction). The uncalled
`build_local_tool_handlers` factory is gone; live local composition uses the
Control dispatcher plus proxy-owned `LocalDataPlane`, as its split-mode tests
prove. At that checkpoint executable ratchets enforced the 40,850 brain ceiling, a 100-line
Surface-handler ceiling, and a 470-line HTTP-view ceiling.

### Tracking follow-up slice

The follow-up migrates `mlflow.context`, `experiment.get_state`, and
`mlflow.finalize_run` into application-owned query/command objects. Surface now
registers thin bound delegates and no longer imports MLflow for tool handling;
the corresponding exact layer exception was deleted. Research returns the
exact stored `experiment.mlflow_run_refreshed` event with canonical readback
state, and the command dispatches that event synchronously to the late Feed
advisory after assembling the response. An explicit foreign-run finalize keeps
its historical response/advisory without fabricating a ledger event or changing
canonical identity.

The standalone tracking slice removed 164 lines from `tool_handlers.py` but
added a 240-line typed application boundary plus the smallest tracking-port,
Research facade, response DTOs, and event-return plumbing. Combined integration
then removed the obsolete local factory, shared reaction registration through
composition, and moved composite reads inward. Characterization covers exact
responses, credential audience, failures, foreign runs, durable event identity,
and direct/MCP ledger parity.

### Final Surface ownership slice

The completion pass moved the remaining immediately actionable policy inward:

- `application/queries.py` now owns compute-cost hydration and experiment,
  project, and reflection logic-graph selection/parsing/lint/ref resolution;
- `ArtifactsFacade` owns submitted resource-version and figure presentation,
  while HTTP retains only MIME/header serialization;
- `application/tool_commands.py` owns merged project, experiment-list,
  resource-find, and storage action decisions; the tool handler is a registry;
- `application/maintenance.py` owns cross-component cleanup, while an
  application query joins a Kernel event count to Sandbox-owned generation
  accounting for tenant counters;
- Research and Artifacts own their review/resource vocabulary validation;
- project membership validation/mutation moved from HTTP into `ProjectService`.

Surface no longer carries an application-layer file override. The only
remaining exact Surface layer exceptions are authentication/configuration and
tool-contract vocabulary seams; the separate Feed-to-unfurl exception is also
unchanged. These are explicit future seams, not hidden orchestration paths.

## Verification gates

The following compatibility matrix is mandatory, not illustrative:

| Area | Characterization that must remain true |
|---|---|
| start, no existing run | transition commits first; successful create is persisted and returned |
| start, existing run | no create call; exact transition state is retained |
| start, adapter error result | running transition remains committed; normalized error is persisted/returned |
| start/retry, tracking persistence raises | error propagates after `experiment.transitioned` committed; no feed phase |
| retry, open run | reuse same run and attempt; no create call |
| retry, terminal/failed prior run | create replacement for same attempt and return its exact persisted state |
| submit/complete | plugin-owned open run requests `FINISHED` |
| abandon | plugin-owned open run requests `KILLED` |
| mark failed | plugin-owned open run requests `FAILED` |
| terminal adapter or persistence failure | suppressed; committed transition/input state retained; feed phase still runs |
| non-plugin/already-terminal/no run | no finalize call |
| tracking unconfigured | no exhibit query, context unconfigured, no run creation |
| public tracking URI only | logging/readback true, control false; no plugin create/update |
| server URI only | logging false, control/readback true; no credential logging env |
| both URIs | create/update/read/context behavior unchanged |
| exhibit pin succeeds | verdict then resource version/association commit before transition |
| exhibit pin raises | already-recorded verdict remains; no transition event; original error propagates |
| submit gate fails after pin | verdict/pin remain; no transition event or post-transition reaction |
| exact ledger sequence | event type, target, canonical payload, count, and order match current start/submit/complete cases; no dispatch rows |
| transaction rollback | forced event-insert failure rolls state and event back together |
| event return | returned ID/timestamp/payload equal persisted row on SQLite and real Postgres |
| dispatch state | exact transition state is threaded; no reload; tracking result becomes response state |
| dispatch recursion | tracking-created/refreshed events are not recursively dispatched |
| feed timing | tracking -> response/context/exhibit assembly -> feed query; assembly failure skips feed |
| feed mapping | final threaded state status selects note; nonterminal state does not call feed |
| feed failure/dedupe | failure suppressed; a post appearing before the late query suppresses the note |
| credentials | MCP transition present, REST transition present, ordinary UI GET absent; secret values absent from logs/events/snapshots |
| shared delivery | REST and MCP invoke the same use-case instance and have equivalent command/ledger effects |
| catalog | `_tool_catalog.json` remains byte-identical to baseline SHA-256 `45e46fac9ea0a4d97fa12d1fc9b111e1088f862992288ffca01a735b70ee2420` |

The follow-on consolidation replaced independently authored plane, hidden,
feature, and handler registries with `TOOL_MANIFEST`. Its generated private
proxy projection carries routing metadata without changing the public catalog
bytes. The stdio proxy now separates pure routing, HTTP transport, credentials,
and project-link resolution; local enrichment reuses the already-fetched
control facts rather than issuing a second `sandbox.get`.

Focused tests additionally cover:

- `record_event` returns exactly the persisted row on SQLite and Postgres;
- state and event roll back together on insertion failure;
- dispatcher registration, phase/order, unknown-event no-op, duplicate name,
  state threading, and propagation semantics;
- handlers receive a committed/readable event;
- byte-for-byte representative transition responses before/after extraction;
- no dispatch acknowledgement changes SSE cursors, ETags, feed cadence, or
  event counts.

Final gates:

```text
cd merv && MERV_VERIFY_PYTHON=python ./scripts/verify_application_architecture.sh
```

`MERV_REQUIRE_POSTGRES_TESTS=1` is added as a fail-closed mode: Docker/Postgres
unavailability is an error rather than a skip, and the event-returning contract
runs against a real Postgres identity column. The repository currently has no
checked-in CI workflow; the implementation adds a single fail-closed verification
script suitable for the project runner and treats its successful execution as a
merge gate. A skipped Docker test in the ordinary suite does not satisfy this
gate.

The verification interpreter can be overridden with `MERV_VERIFY_PYTHON`; it
must have the project test dependencies installed. The plane-layout test itself
also launches `/usr/bin/python3` to prove that local client/proxy/shared modules
remain usable below the brain's packaged Python floor; that interpreter does
not need pytest.

The complete suite must run outside filesystem/network confinement if tmux or
loopback tests fail only because of sandbox permissions. `git diff --check`, a
cross-module import inventory, and a clean worktree/commit audit close the work.

## Workflow/read-side consolidation

Workflow orientation is now an Application read model rather than a
Research-owned orchestration service. `WorkflowQuery` joins the stable
`ResearchSnapshots` and `SandboxReads` contracts, while
`ProjectDashboardQuery` reuses that same project snapshot for Home and the
compact project orientation. `ResearchSnapshotReader` hydrates each requested
experiment at most once per query and gathers reflection/review facts in the
same Research transaction. `NextActionPolicy` is pure: it receives captured
records and emits the byte-compatible workflow payload without SQL, service
calls, or side effects.

The superseded `research_core/workflow.py`, `workflow_views.py`,
`project_overview.py`, and Kernel workflow-reader protocols were deleted.
Characterization covers the rich HTTP shape, slim MCP projection, reflection
takeovers, review gates, active Sandbox summaries, exact legacy parity, and the
one-hydration dashboard invariant.

## Four-module consolidation rewrite

The follow-on rewrite makes four formerly implicit boundaries executable:

- Workflow and dashboard reads now use the Application-owned query, bulk
  Research snapshot, pure next-action policy, and Sandbox read facade described
  above.
- `TOOL_MANIFEST` is the one source for schema, visibility, project scope,
  execution strategy, features, and handler identity. The stdlib proxy is split
  into a 79-line composition edge, MCP shell, manifest router/gateway, HTTP
  transport, credential provider, and project resolver.
- HTTP authentication, project authorization, and tool invocation live in an
  explicit gateway; the formatter-clean FastAPI factory is 122 lines and route
  modules remain delivery-only. URL scope is bound after body parsing and a
  contradictory body scope is rejected, so authorized path identifiers cannot
  be replaced before gateway invocation.
- Sandbox now exposes a 294-line stable facade over typed command/query values
  and cohesive handlers. `SandboxRepository` owns every row read/write; the
  pure lifecycle reducer returns event facts and side-effect intents for
  reconcile, reap, and explicit release; and the composition root owns
  provisioners and daemons. Provisioner settle paths still invoke
  `SandboxLifecycle` directly. Provider drivers, including Modal's explicit
  non-VM path, are unchanged.

Formatter-clean structure has a real size cost: the tree is **41,389 brain lines**, 541
above the 40,848 pre-consolidation checkpoint. The old orchestration hubs still
shrank by more than 500 lines in aggregate; the increase is the explicit DTO,
port, reducer, and runtime structure that replaced hidden coupling. The new
41,389 landed-checkpoint ceiling bounds that cost, while tighter per-hub ratchets cap the
workflow group at 1,600 lines, the Sandbox facade at 300, its handlers at 1,050,
the HTTP factory/gateway pair at 500, and the proxy composition/gateway/shell at
100/350/120. This avoids rewarding line compression while preventing policy
from growing back into the delivery facades.

## Non-goals and follow-up queue

- no background event worker, delivery checkpoint table, retries, or exactly-once
  claim;
- no automatic feed publication;
- no change to the metrics-exhibit gate or transaction cuts;
- no wholesale wrapper around every method in every service;
- no physical relocation of MLflow/object-storage files merely for taxonomy;
- no asynchronous conversion of HTTP/MCP reads or sandbox lifecycle calls that
  already delegate to one owning service.

No additional use case is required to close this migration. Remaining exact
layer exceptions and any further resource or UI work
are optional targeted improvements; each should begin only when a concrete use
case justifies the smallest new facade verb or port operation.

## Adversarial review disposition

Initial verdict: **NO-GO**, with all findings incorporated for re-review:

- split component ownership from layer role with file-exact laws and shrinking
  exceptions;
- explicitly preserve and test the current REST/MCP credential behavior;
- require canonical `INSERT ... RETURNING`, deep immutable events, and a
  fail-closed real-Postgres gate;
- replace dict/`Any` facade claims with typed slice protocols, capability truth
  table, fake conformance tests, and real composition identity tests;
- defer the false `SandboxService` facade;
- add the full branch/ordering/failure/credential/catalog matrix;
- thread the exact state instead of reloading it;
- preserve tracking -> response assembly -> feed timing and map feed from the
  final threaded state.
- keep Research-owned persisted-run/slim DTOs separate from Application-owned
  tracking/response DTOs, with an explicit translation at the use-case edge;
- explicitly authorize password redaction in telemetry before extraction while
  preserving credential-bearing public transition responses.

Final re-review verdict: **GO**. No contradictory or unimplementable
requirements remained after the revisions above.

Implementation re-review verdict: **GO**. The final tree meets the re-ratified
LOC and Surface/wrapper ratchets, preserves the proxy catalog, passes the
fail-closed Postgres gate, and proves equivalent REST/MCP responses plus exact,
non-recursive start/submit/complete ledger effects.
