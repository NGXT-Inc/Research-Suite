# Module Boundaries

The brain is a modular monolith. Two independent classifications describe it:

- a **component** says which capability owns a file;
- a **layer** says what architectural job that file performs.

This distinction is intentional. Research, Artifacts, Sandbox, and Feed are
business components. MLflow is an outbound tracking integration, and concrete
object storage is outbound infrastructure. A provider driver can therefore be
adapter-layer code owned by Sandbox; a folder name does not make an adapter a
business authority.

```text
                       bootstrap / composition
                    /          |             \
                   v           v              v
              delivery --> application <-- adapters
                                  |
                                  v
                         component facades/ports
                                  |
                                  v
                                kernel
```

The pure `merv.shared` package sits outside this brain-only law.

## Component law

Every `src/merv/brain/**/*.py` file has exactly one component. Deepest-prefix
classification plus file overrides handles mixed packages.

| Component | Physical code today | Meaning |
|---|---|---|
| Kernel | `kernel/**` | shared contracts, state floor, IDs, events, utilities |
| Research | `research_core/**` | experiment/review/reflection/project authority |
| Artifacts | `artifacts/**` | submitted artifacts, upload tokens, pinned evidence |
| Sandbox | `sandbox/**` | lifecycle and provider-driver capability |
| Feed | `feed/**` | feed records and advisory policy |
| Application | `application/**` | cross-component commands, reactions, and composite reads |
| Tracking integration | `mlflow/**` | MLflow implementation of tracking ports |
| Storage | `object_storage/**` | byte/object adapters plus the legacy ledger service |
| Surface | `surface/**` | HTTP/MCP delivery and the co-located composition root |

The exact component import matrix is:

| Importer | May import |
|---|---|
| Kernel | Kernel |
| Research | Research, Artifacts, Kernel |
| Artifacts | Artifacts, Kernel |
| Sandbox | Sandbox, Kernel |
| Feed | Feed, Kernel |
| Application | Application, Research, Artifacts, Sandbox, Feed, Kernel |
| Tracking integration | Tracking integration, Application, Kernel |
| Storage | Storage, Application, Kernel |
| Surface | any component; its independent layer classification still applies |

Outside bootstrap, code enters another component only through its declared
`facade.py` or `ports/**` entrypoint. This is the executable form of “one stable
public facade”; it prevents a new use case or adapter from depending on internal
services. The legacy public-entrypoint exception ledger is now empty: every
cross-component import must enter through a facade or port. Workflow reads use
`ResearchSnapshots` and `SandboxReads`; Sandbox commands use the separately
declared `Sandbox` facade.

Research reaches immutable artifact evidence only through semantic DTOs on
`artifacts/ports/EvidenceReader`; it never sees persistence rows, Artifact
tables, or blob locators. Artifacts resolves association targets through the reverse
`AssociationTargetResolver` port. Both concrete adapters use read-only store
connections while the calling command holds the monolith's single-writer lock,
so the public contracts remain connection-free without weakening consistency.

## Layer law

The initial layer mapping is deliberately honest about mixed directories:

| Layer | Representative paths |
|---|---|
| foundation | `kernel/**` |
| port | `kernel/ports/**`, `application/ports/**`, `artifacts/ports/**`, `sandbox/sandbox_backend.py` |
| domain | `research_core/domain/**`, pure artifact/feed policy files |
| application | component services and cross-component work under `application/**` |
| adapter | `mlflow/**`, concrete storage/blob code, sandbox provider drivers, client/runtime adapters |
| delivery | ordinary `surface/**` HTTP/MCP/auth/serialization code |
| bootstrap | Surface composition/config/control wiring, the HTTP process launcher, sandbox driver registration |

`object_storage/service.py` is a notable override: it owns versioning, TTL,
deduplication, lifecycle events, concurrency, and reclamation policy, so it is
Storage-component **application** code, not a provider adapter. Its physical
move is deferred. Conversely, `object_storage/{blobs,s3_blobs,s3_object_store}`
are adapters implementing kernel-owned storage ports.

Imports must point inward:

- foundation -> foundation;
- port -> port/foundation;
- domain -> domain/port/foundation;
- application -> application/domain/port/foundation;
- adapter -> adapter/application/domain/port/foundation;
- delivery -> delivery/application/port/foundation;
- bootstrap -> any layer.

Nothing except bootstrap may import bootstrap, and no non-delivery layer may
import delivery. `LAYER_EXCEPTIONS` is empty, so every newly detected cross-layer
edge fails immediately; there is no wildcard or compatibility allowance.

## Ports and adapters

Research/application workflows depend on an `ExperimentTracking` port, not on
MLflow. `CentralMlflowService` implements it. The port distinguishes logging,
control, and readback capabilities so tracking-only and server-only deployments
retain their current behavior.

`mlflow.context`, `experiment.get_state`, and `mlflow.finalize_run` enter through
application-owned query/command objects. Surface registers their bound methods
and does no tracking policy or persistence coordination. A canonical finalize
returns its exact `experiment.mlflow_run_refreshed` event from Research and
synchronously dispatches the Feed advisory after response assembly. Finalizing
an explicit foreign run keeps the old advisory response but writes no event and
never changes the experiment's canonical run identity.

Artifacts, Feed, cleanup, and storage-ledger policy depend on narrow blob/object
ports owned by Kernel. Application response composition depends on its batch
`ProducedObjectCatalog` port; the provider-independent SQL catalog under
`object_storage` implements that port so historical ledger rows remain readable
when no byte provider is enabled. Local and S3 implementations remain under
`object_storage` as replaceable adapters. Old import paths may re-export the
same symbols for compatibility, but do not own their definitions.

Operator-triggered cleanup is a cross-component use case in
`application/maintenance.py`; Surface only exposes its injected entry point.
An Application query combines a Kernel-owned tenant event count with
Sandbox-owned generation counters and injects the result into admin delivery.

The declarative `TOOL_MANIFEST` owns tool schemas, visibility, scope, execution,
features, and handler identities. Surface derives its control handlers from
those identities; every tool is a control tool served by the brain, so
hidden/handler routing is not separately maintained. The transition
adapter still sets the agent credential audience. Merged project,
experiment-list and storage decisions live in
`application/tool_commands.py`.

These are dependency changes, not service extraction: everything still runs in
one brain process and shares the existing transaction/event ledger.

## Synchronous reaction model

The composition root owns one in-process reaction registry. Application use
cases dispatch exact durable event values explicitly; there is no ledger scan,
worker, replay loop, or second event stream. Fatal handlers stop a phase and
propagate. Advisory handlers, currently Feed reminders, yield no outcome when
they fail and cannot break the primary command or query.

The executable catalog is the registry's only registration source. Each entry
names its producer, payload version, transaction boundary, reaction phase,
handler, failure mode, and redelivery requirement. Structure tests resolve the
producer and transaction methods and prove that runtime registrations exactly
match the catalog.

Transition reactions run immediately after their committed command. Canonical
tracking finalization dispatches its exact `experiment.mlflow_run_refreshed`
event after response assembly. The explicit foreign-run compatibility path has
no durable event and therefore calls its advisory directly. The review verdict
reminder deliberately runs later, when the producer reads `review.status`, but
uses the existing `review.submitted` event ID. Repeated reads are allowed and
handlers must be repeat-safe. Any future asynchronous delivery needs durable
checkpoints keyed by `(event_id, phase, handler_name)`, plus an external-side-
effect idempotency policy, before a worker is introduced.

## Composite query model

Composite UI reads likewise belong to Application. `application/workflow.py`
assembles workflow orientation and the project dashboard from one bulk Research
snapshot plus Sandbox reads; `application/queries.py` owns tracking overview,
experiment figure, hydrated compute costs, and experiment/project/reflection
logic graphs. Artifacts owns
submitted artifact-content and figure selection behind `ArtifactsFacade`;
Application owns hosted content-response and experiment/figure presentation.
Surface retains authentication, conditional HTTP caching, local-field
redaction, MIME/header shaping, and serialization only.

The bulk snapshot is backed by plural Research state/gate reads and the
connection-free `EvidenceReader.artifacts_for_targets` port, which chunks exact
IDs in groups of 400. A project dashboard therefore uses 22 database reads for
both one and 25 experiments. Full Artifact pages use six reads, Research graph
references use at most one read per reference type, and MLflow overview uses at
most three remote calls. Reflection history is the deliberate exception: its
frozen response returns every rich historical wave, so it remains linear and
has explicit query ceilings for representative 25-wave abandoned and published-
graph histories instead of a false constant-query claim. A future summary or
paginated contract should precede batching that endpoint.

Research evaluates each experiment or reflection gate once per hydration. The
typed, JSON-safe evaluation carries requirement, validation, current-snapshot
review-request, blocker, and legal-transition facts. That same value enforces
transitions, supplies the semantic checklist, travels in `ResearchSnapshot`,
and drives workflow guidance. Application may combine those facts with live
Sandbox state for presentation, but it cannot reconstruct transition legality.
Review requests likewise read their expected role from the current evaluation;
there is no parallel status-to-review-role map.

Research gate contracts contain semantic roles, evidence status, domain
enforcement errors, human-readable transition preconditions, blocker codes,
and legal transitions. They do not choose an executable next action.
Application owns the tool names, skills, ready actions, templates, and recovery
wording exposed by `status_and_next`, plus the compatibility projection that
adds those fields to experiment and reflection checklists at the public
response boundary. Reflection drift signals likewise contain facts only;
Application derives their agent hint and post-publish actions.
`StatusAndNextQuery` joins one Research snapshot with Sandbox and
produced-object facts before applying that pure guidance policy.

Review role/verdict validation is Research domain policy; artifact association
role/target validation is Artifacts domain policy. Project membership mutation
is owned by `ProjectService`, not by an HTTP route.

## Cross-package law

Brain code may import pure `merv.shared` contracts. Shared code imports only the
standard library and itself. The onboarding client ships in the slim bundle and
imports only the standard library and `merv.shared`, never `merv.brain`.

## Executable ratchets

`tests/structure/test_module_boundaries.py` AST-scans top-level and
function-local imports, classifies every brain file twice, enforces both laws,
checks component-owned SQL, and rejects stale table entries and stale exception
pairs. Every stable table has an explicit owner; an unclassified new table
fails closed. SQL may name only tables owned by the file's component, Kernel
tables, or tables behind a ratified component dependency. Research has a
zero-entry foreign-Artifact-table counter, so any new direct evidence SQL fails
immediately. The public-entrypoint exception ledger must shrink whenever a seam
is repaired.

Application has a zero-exception purity check: it may not import delivery,
concrete adapters, frameworks, database/network SDKs, environment access, or
state/config modules or state-store types; accept persistence parameters; open
connections/transactions; or contain SQL. Non-bootstrap code may not construct
another component's concrete collaborator. Surface delivery has zero-baseline,
fail-closed scans for raw implementations, persistence reach-through, and
whole-app dependency carriers.

Untyped collaborator declarations are separate from JSON payload debt. A
shrinking dependency ledger records the remaining callable seams and injected
adapter test doubles. New `Any` or generic `Callable` collaborators fail;
repaired entries must be removed from the ledger.

Public boundary value objects—including exported Application response/event
values—are discovered by structure tests, normalized to JSON primitives, and
round-tripped with strict finite-number handling. A complete sample registry
prevents new DTOs from escaping the test. Untyped fields and non-string mapping
keys are an exact shrinking debt ledger; there is no remaining JSON-roundtrip
exception. Concrete connections, cursors, stores,
repositories, and services are never permitted in boundary values.

Sandbox provider neutrality is enforced separately: services do not dispatch
on provider-name literals. Capability flags and the typed `SandboxDriver` /
`SandboxManagementTransport` contracts express provider differences; lazy
provider descriptors form the composition registry; the shared offline driver
conformance suite applies to every registered implementation.

Sandbox's public facade accepts a composition-owned `SandboxRuntime` rather
than reconstructing repositories, lifecycle services, provisioners, daemons,
or keys. Public calls become typed commands/queries; command,
query, projection, and maintenance handlers own their respective logic. The
runtime owns thread start/shutdown and `SandboxRepository` owns SQL. The pure
lifecycle reducer translates reconcile, reap, and explicit-release observations
into terminal event facts and ordered side-effect intents. Provisioner settle
paths continue to route terminal writes through `SandboxLifecycle` directly.
Sandbox read queries scope rows by both project and experiment. Production code
may not reach through `app.sandboxes` to repositories or runtime collaborators;
bootstrap uses the separately owned runtime when it needs lifecycle internals.
