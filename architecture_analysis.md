# Architecture Analysis — research-suite

_Generated 2026-06-21 by a 90-agent multi-agent review: 11 source agents (9 code regions + 2 global lenses) each graded all 5 rubric points, every finding was adversarially verified by an independent skeptic, and a lead-architect agent synthesized the survivors. **48 findings survived verification; 30 were refuted.** Judged against [Architecture_Rubric.md](Architecture_Rubric.md)._

## Overall grade: **A-**

> This is a mature, unusually well-architected codebase whose dependency graph and plane separation are genuinely exemplary (acyclic seven-layer stack, machine-enforced import bans, derive-from-one-table idioms, profile-gated OSS reuse with disciplined documented NIH) - separation, comments, and OSS reuse all sit at A/A-. The material work left is almost entirely brevity-grade kill-duplication, not structural: the same correctness-critical policy (reflection drift, snapshot-id pinning, synthesis role resolution, rsync command building) is copy-pasted across two homes and must stay byte-identical to stay correct, which is the chief remaining drift risk. Tackle the duplicated invariants first (P1), trim the dead/shim surface and the lone fat http_api.py module, and the codebase clears a solid A.

## Scorecard

| # | Rubric dimension | Grade | Rationale |
|---|---|---|---|
| 1 | Modular architecture | **A-** | Across every region the dominant pattern is genuine one-concern-per-module decomposition that is testable in isolation: domain modules are AST-enforced pure-policy leaves (graph_lint imports only json), the sandbox facade delegates to single-responsibility collaborators (registry/provisioner/daemons/metrics/parachute/sync_sessions), figure_view is a textbook pure projection, and the frontend shares a single Feed.jsx and 12 components across desktop and mobile rather than forking. The recurring blemish is over-modularization of single-implementer reader/writer ports in ports/ (synthesis_writers, review_policy, the 4 workflow_readers) flagged by the global lens, plus a handful of dead/test-only shim modules (services/reflection_policy.py, execution/{errors,types}.py). The one fat module is http_api.py (1748 lines fusing a view-helper class with a route factory), but lifted route modules and structure tests keep it cohesive rather than a god-file. |
| 2 | Separation of concerns | **A** | This is the codebase's strongest dimension and the regions agree: a full Tarjan SCC scan confirmed a strictly acyclic seven-layer stack (domain -> ports -> services -> {dataplane,execution,state} -> composition -> transport) with every cross-layer edge pointing one direction toward stable narrow leaves (utils fan-in 55, store.py behind a 14-method Protocol surface, pure contracts.py). The boundaries are unusually machine-enforced by AST import-ban lints (the load-bearing 'cloud cannot see local FS' rule is tested, not just documented), mode decisions are centralized in named policy tables (HttpSurfacePolicy, contract capability flags) instead of scattered auth-is-None ternaries, and ports that genuinely earn their keep carry >=2 implementers across modes. The only real deductions are localized: an internal 'synthesis' vs external 'reflection' rename leaks across ~43 call sites instead of one adapter, and a small data-only contracts<->feed_contracts cycle. |
| 3 | Code brevity | **B+** | Logic is consistently dense and earns its lines, with error handling scoped to real failure modes rather than just-in-case coverage (frontend, services-core, domain all called out for no speculative bloat). The dimension is dragged to B in the implementation-heavy regions by a consistent duplication pattern: drift/snapshot-id policy duplicated across experiments/syntheses, ssh_rsync's push/pull and sync/push_initial near-verbatim duplicates (~150L collapsible), graph parse-and-lint blocks repeated in http_api, the MCP catalog-fetch loop triplicated, and synthesis role-resolution duplicated verbatim across desktop/mobile. Dead code (pinned_version_row, _get_version, _pulled_mlflow_db_path, the reflection_policy shim) recurs as minor surface area. Domain/contracts and frontend rate highest (A/A-); execution and services-core lowest (B). |
| 4 | Comments are one-liners | **A** | Comment discipline is exemplary and remarkably uniform across regions: comments overwhelmingly encode the WHY the code cannot show (ctime-in-version-token defeating mtime-preserving edits, mawk 32-bit printf clamp, gVisor cgroup host-scoping, Apple rsync 2.6.9 protocol break, blob-auth-for-images, Zustand getSnapshot identity contract, SVG-XSS re-host guard) rather than restating code, and try/catch targets known failure modes. The only consistent friction with the literal 'one-liner' wording is that several of the best rationale comments are multi-line docstrings/blocks (synthesis_gates FSM, SSRF TOCTOU note), but each line is load-bearing, so the rubric's operative test (say what code can't) is fully met. |
| 5 | Reuse open source | **A** | The most consistently excellent dimension: every commodity capability with a good OSS fit is delegated to a maintained, lightly-imported, profile-gated dependency (FastAPI/uvicorn/httpx/pydantic, boto3 for S3 SigV4/presign, psycopg for Postgres via a thin string-translation dialect, modal lazily, and react-markdown/@xyflow/react/prism/zustand on the frontend), with no date/util mega-libs and a 6-package backend base. Every hand-roll falls into a rubric-sanctioned bucket: deliberate documented stdlib-only NIH that is machine-enforced (the MCP stdio proxy via test_mcp_server_imports_only_stdlib, the stdlib-only SSRF unfurl guard) or genuinely CORE logic no library fits (workflow/gate policy, transfer contract, figure layout). No heavy dependency is pulled for a few-line job. No region scored below A. |

## Region × dimension grades

| Region | Modu | Sepa | Brev | Comm | Oss_ | Findings |
|---|---|---|---|---|---|---|
| services-core | B+ | B | B | A- | A | 4 problems |
| services-feed | A- | A- | B | A | A | 2 problems |
| services-sandbox | A- | A- | B | B | A | 2 problems |
| transport-composition | B+ | A- | B | A | A | 1 problems |
| domain-ports-contracts | A- | A | A- | A | A | 0 problems |
| state-dataplane | A- | A | B | A- | A | 0 problems |
| execution-infra | B+ | A- | B | A | A | 3 problems |
| mcp-server | B+ | A- | B+ | A | A | 2 problems |
| frontend | A- | A | A | A | A | 1 problems |
| lens-separation | A- | A- | N/A | N/A | N/A | 0 problems |
| lens-oss | N/A | N/A | A- | B+ | A | 0 problems |

## What the architecture gets right

- Strictly acyclic, one-directional seven-layer dependency graph (domain -> ports -> services -> {dataplane,execution,state} -> composition -> transport), verified by a full Tarjan SCC scan, with high fan-in converging on stable narrow leaves (utils, store.py's 14-method Protocol surface, pure contracts.py) - the rubric's exact target shape for separation of concerns.
- Architectural intent is machine-enforced, not merely documented: AST import-ban lints in tests/structure/ keep domain modules as pure leaves and uphold the load-bearing 'cloud control plane cannot reach the local filesystem' rule, and the MCP proxy's stdlib-only invariant is pinned by test_mcp_server_imports_only_stdlib.
- Derive-from-one-table idiom eliminates whole classes of drift: TOOL_CONTRACTS in contracts.py projects routing tables, the tool registry, and plane membership, while GATE_TABLE projects the FSM transition graph and requirements - routing, registry, and state machine cannot diverge because each is a projection of a single source of truth.
- Exemplary build-vs-buy judgement: heavy commodities are delegated to profile-gated OSS (FastAPI/pydantic/boto3 SigV4/psycopg) while genuinely CORE or daemon-constrained logic stays a lean stdlib hand-roll (the SSRF-guarded unfurler, the MCP stdio proxy, the transfer contract) - both rubric guardrails honored with no heavy dep pulled for a few-line job.
- Pure projection and single-concern collaborators that are testable without loading the system: figure_view.py (no DB/IO, inputs handed in by HTTP), the sandbox facade's registry/provisioner/daemons/metrics/parachute split, control-plane-only services (quotas/cleanup/identity) dormant-by-construction in local mode, and a thin frontend api seam the feed depends on one-directionally.
- Comments consistently carry hard-won WHY the code cannot show (ctime version token, mawk 32-bit clamp, gVisor cgroup scoping, Apple rsync protocol break, blob-auth image fetch, Zustand identity contract) - a uniform bar across backend and frontend.

## Findings by region

_Only findings that survived adversarial verification are listed. Severity is the verifier-adjusted value._

### Core services (workflow/experiments/reviews/syntheses/resources/projects/claims)

Core services are well-modularized one-responsibility units with standout WHY-focused comments and disciplined stdlib reuse; the gated-artifact and projection concerns are cleanly factored out. The main weaknesses are duplication of the reflection-drift and snapshot-id policies across experiments/syntheses, a synthesis-vs-reflection naming split whose translation leaks across ~40 call sites rather than living in one adapter, and minor dead code; single-implementer ports add some ceremony but are defensible as the mechanism enforcing an acyclic workflow boundary.

**Problems**

- **[medium · brevity]** Reflection-drift policy computed in two places that can disagree
  - _Evidence:_ services/experiments.py:218-251 (_terminal_experiments_since_last_reflection) re-derives terminal-minus-covered drift that services/syntheses.py:1277-1355 (reflection_signal) already computes against the same published corpus_json.
  - _Fix:_ Have experiment.create's block check call syntheses.reflection_signal (or a shared domain helper) for new_terminal_since_publish + the open-wave id, deleting the parallel SQL+corpus-parse in experiments.py so the threshold logic has one source of truth.
- **[medium · modularity]** target_snapshot_id duplicated across experiment and synthesis services
  - _Evidence:_ experiments.py:717-731 and syntheses.py:1204-1218 are byte-near-identical: same resource-token format string and the same status|attempt|sorted-tokens join, differing only in the 'experiment'/'synthesis' literal.
  - _Fix:_ Extract one snapshot_id(target_type, target_state) helper (alongside pinned.py) taking the hydrated dict; both services call it. Removes a divergence risk in the review-pinning invariant.
- **[medium · separation]** Internal 'synthesis' vs external 'reflection' naming forces scattered translation
  - _Evidence:_ ~43 synthesis<->reflection swaps across workflow.py (_external_reflection_signal:616, gate=='synthesis_review'->'reflection_review':374, _synthesis_workflow_for), reviews.py (_hydrate_request/_hydrate_review/_with_snapshot/reviewer_handoff all remap target_type), resources.py:632,930.
  - _Fix:_ Concentrate the synthesis<->reflection boundary rename in a single projection adapter (e.g. extend domain/reflection_projection.py) and route all transport-facing reads through it, so services stop hand-patching the literal at each call site.
- **[low · brevity]** Dead helpers never called
  - _Evidence:_ pinned.py:114-141 pinned_version_row is defined but has zero call sites; resources.py:935-947 _get_version is private and unreferenced.
  - _Fix:_ Delete both; they are just-in-case surface area the gates don't use.

**Strengths**

- Comments consistently carry the WHY the code can't show — resources.py:845-849 (ctime in version token defeats mtime-preserving edits), experiments.py:536-548 (system transition tolerated no-op rationale), syntheses.py:985-991 (materialize only post-review so speculative edits can't leak).
- Control-plane-only services stay mode-blind and testable — quotas.py, cleanup.py (clock-injectable run_all), transcript_cache.py, identity.py are each self-contained with injected clocks/stores and documented local-mode no-op invariants.

### Feed + view/projection services

A strong region. feed_unfurl.py and figure_view.py are exemplary: the unfurler is a self-contained, stdlib-only, defensively-correct SSRF hostile-input boundary, and figure_view is a pure projection with no DB/IO. feed.py is large but cohesively sectioned and mode-blind (rejects local image paths in control mode, hides blob hashes from clients). The main rubric blemishes are an over-built feed.post/post_observed/validate_post_intent triad and a dead reflection_policy.py compatibility shim with zero importers that exists only to satisfy its own structure test.

**Problems**

- **[low · brevity]** Dead reflection_policy.py shim exists only to satisfy its own test
  - _Evidence:_ services/reflection_policy.py:1-13 re-exports two thresholds, but a repo-wide grep shows zero importers; real consumers (services/experiments.py:12, services/syntheses.py:22) import straight from domain.reflection_policy. The only reference is test_service_layout.py:477 asserting the shim is a shim.
  - _Fix:_ Delete the module and its test; it is a compatibility shim guarding against no caller (creation commit dbe3363, thresholds already moved to domain in 3d423d6).
- **[low · separation]** reflection_tools rename mapping is split across adapter and domain
  - _Evidence:_ reflection_tools.py:53-57 hardcodes submit_reflection_artifacts->submit_synthesis, while the reverse mapping lives in domain/reflection_projection.py:16-27 (external_reflection_state).
  - _Fix:_ Move the inbound transition rename into reflection_projection too so the external<->internal vocabulary lives in one place; the adapter then just delegates.

**Strengths**

- Stdlib-only SSRF-guarded unfurler is exactly the right build-vs-buy call — feed_unfurl.py:28-156 - ipaddress/socket/urllib/HTMLParser only; _host_is_public re-validates every resolved IP, redirects followed manually with re-validation per hop, body/redirect/timeout caps.
- Comments document non-obvious security/architecture WHYs — feed.py:417-423 (re-host only raster thumbnails, external SVG would be stored XSS), feed.py:558-560 (exclude feed.* events from nudge), feed_unfurl.py:20-23 (DNS-rebinding TOCTOU acknowledged with hardening path).
- figure_view is a textbook pure projection — figure_view.py:1-13 docstring + 70-293: no DB/backend imports (only ACTIVE_SANDBOX_STATUSES const), all inputs handed in by the HTTP caller; nested add_edge/clamp_attempt keep helpers local to build_experiment_figure.
- graph_refs resolves N ref kinds through one data table, not a switch — graph_refs.py:11-72 - GraphRefType dataclass + GRAPH_REF_TYPES tuple drive _resolve_one/_record_ref generically; adding a ref kind is a one-row edit, and path-refs fall back without probing local files (control-plane-safe, line 134-139).
- Feed owns its own dialect-neutral schema to stay liftable and mode-blind — feed.py:42-85 FEED_SCHEMA lives with the service (not shared SCHEMA), TEXT/INTEGER only so it runs on SQLite and Postgres; _post rejects local image_path so the control plane never touches the user FS (feed.py:195-198).

### Sandbox subsystem (provision/registry/metrics/parachute/sync/views/daemons)

The sandbox subsystem is a well-executed decomposition: a 1301-line facade delegates to focused collaborators (registry=persistence, provisioner=job lifecycle, daemons=threads, metrics, parachute, sync_sessions=leases, views=pure projections), each testable in isolation and wired only at composition time through ports that genuinely earn their keep (2+ implementers across local/control/daemon modes). Separation is enforced by AST structure tests and an inversion hook that keeps the registry persistence-only. The main rubric costs are brevity/comment ceremony: verbatim duplication of _tenant_for_project across the facade/provisioner seam, a dead pass-through helper, a redundant request wrapper, and several multi-paragraph plan-citation comments that exceed one-liner discipline.

**Problems**

- **[low · brevity]** Verbatim duplication of _tenant_for_project across the facade/provisioner seam
  - _Evidence:_ sandboxes.py:1210-1219 and sandbox_provisioner.py:461-469 are byte-identical: both connect, SELECT tenant_id FROM projects, default 'local'.
  - _Fix:_ Hoist to the registry (it already owns the store and projects reads, e.g. upsert at :175-182) as registry.tenant_for_project; both callers delegate. Kills duplication and centralizes the projects-table read.
- **[low · brevity]** Dead facade helper _pulled_mlflow_db_path
  - _Evidence:_ sandboxes.py:1113-1117 — only caller in production is the worker calling its OWN pulled_mlflow_db_path (worker.py:449); the facade method is referenced only by a test (test_sandbox_service.py:590).
  - _Fix:_ Delete the facade method and update the test to assert on the worker directly; it is a pure pass-through to worker.pulled_mlflow_db_path with no facade-side value.

**Strengths**

- Facade decomposes cleanly into single-concern collaborators — sandboxes.py:164-237 wires SandboxRegistry (persistence), SandboxProvisioner (jobs), SandboxDaemons (threads), SandboxMetrics, SandboxParachute, SyncSessionService; each is a separate file owning exactly one concern.
- Registry stays persistence-only via an outward terminal hook — sandbox_registry.py:22,336-342 — on_terminal hook fires runtime teardown (tunnels/conn files) without the registry knowing what they are; facade wires _on_terminal_row (sandboxes.py:207).
- Lease/session boundary closes the rsync --delete footgun in the contract, not the worker — sync_sessions.py:63-103 build_sync_session embeds direction_policy + transfer_contract_version so the worker refuses bytes under a policy its flags do not implement; leases live in the record store as the sole cross-client authority.

### Transport + composition root (http_api, *_http, app, control_*, project_router, tool_facade)

A large, mostly disciplined transport + composition region. Mode wiring is correctly confined to composition/ (local_mode is a 20L pass-through; control_mode/daemon_mode each own one process role), and the load-bearing "cloud sees no local FS" rule holds via HttpSurfacePolicy flags rather than scattered auth-is-None checks. The chief weakness is http_api.py (1748L), which fuses a ~770L UI view-helper class with a ~880L route factory and a dense route_call_tool decision tree; it stays cohesive routing rather than a god-file thanks to lifted route modules and structure tests, but the view class and the routing factory are two separable concerns.

**Problems**

- **[low · brevity]** Graph JSON-parse-and-lint block duplicated across endpoints
  - _Evidence:_ http_api.py:700-718 (experiment_logic_graph) and http_api.py:795-813 (_graph_payload_for_synthesis) repeat the same json.loads→isinstance(dict)→graph_problems→graph_refs.resolve_index assembly.
  - _Fix:_ Hoist the parse+lint+ref-resolve into one helper (e.g. _graph_payload(text, base, project_id)) and call it from both; removes ~20 duplicated lines and one divergence risk.

**Strengths**

- Mode decisions centralized in named policy tables — http_policy.py:14-73 (HttpSurfacePolicy dataclass + for_surface) and HOSTED_CONTROL_TOOL_POLICIES/HTTP_DATA_PLANE_FEATURE_TO_TOOL; http_api.py consumes surface.* flags and the tables, never 'auth is not None' ternaries (enforced by test_http_surface_policy_keeps_mode_decisions_named).

### Execution (ssh/rsync/transfer) + low-level infra utils

This region is well-separated and exemplarily commented: a stdlib-only neutral SandboxBackend port (AST-enforced against execution/subprocess imports) sits above concrete data-plane machinery, and a single transfer contract feeds both rsync flags and the parachute tar across two backends. The main weakness is brevity inside ssh_rsync.py, where sync/push_initial and _pull/_push_command are near-verbatim duplicates (~150L collapsible), plus two test-only compat shim modules (execution/errors.py, execution/types.py) that add indirection with no production caller. OSS reuse is judged correct rather than NIH: rsync/ssh/tmux/curl are the right primitives and are version-gated, not reimplemented.

**Problems**

- **[medium · brevity]** ssh_rsync push/pull command builders are duplicated verbatim
  - _Evidence:_ execution/ssh_rsync.py:337-409 — _pull_command and _push_command are byte-identical (same SSH string, same -az/--delete/--prune-empty-dirs/--itemize-changes/--max-size flag list, same exclude loop) except the final src/dst pair is swapped.
  - _Fix:_ Collapse into one _rsync_command(*, src, dst, max_size, excludes) that builds the shared flags once; pull/push differ only in which of {remote, local} is src. Removes ~35 duplicated lines and one drift risk.
- **[medium · brevity]** sync() and push_initial() share ~95% of their body
  - _Evidence:_ execution/ssh_rsync.py:164-260 vs 262-335 — identical guard checks, start clock, the command-loop (run/append stdout-stderr/tolerate exit 23 for optional/_count_changed), and SshRsyncResult assembly; only the command list and direction label differ.
  - _Fix:_ Extract a private _run_passes(commands, direction) helper that owns the loop + result assembly; sync/push_initial each just build their command list and delegate. Cuts ~60 lines.
- **[low · modularity]** execution/errors.py and execution/types.py are test-only compat shims
  - _Evidence:_ execution/errors.py and execution/types.py only re-export from sandbox_backend; grep shows zero production importers — only tests/sandbox/*.py import via backend.execution.{errors,types} while app code imports backend.sandbox_backend directly.
  - _Fix:_ Point the tests at backend.sandbox_backend and delete both shim modules (and the sync_dirs/types __init__ surface they mirror). Removes indirection that exists purely to keep an old import spelling alive for tests.

**Strengths**

- Comments explain non-obvious platform failure modes precisely — usage_metrics.py:34-38 (mawk %d 32-bit clamp → use %.0f), :59-66 (gVisor host-level cgroup/meminfo unusable as denominator); ssh_rsync.py:25-29 (Apple rsync 2.6.9 protocol-29 break); sandbox_support.py:78-87 (ControlMaster resolves tunnel host once).
- Transfer leans on rsync/ssh/tmux/curl rather than reimplementing them — transfer_spec.build_parachute_script (curl -T + tar + sha256sum), bootstrap_tools.REC_EXEC_CORE (tmux supervisor), ssh_rsync.resolve_rsync (version-gates the OS rsync 3.x).
- Backend port is neutral and AST-enforced — sandbox_backend.py imports only stdlib (dataclasses/typing); test_plane_layout.py:457-461 asserts it imports no 'execution' segment, and :450-455 forces record/control sandbox services onto the port not the execution package.
- One transfer contract feeds rsync flags and the parachute tar — transfer_spec.py:29-108 — DEFAULT_EXCLUDES/size caps drive both ssh_rsync's --exclude/--max-size and tar_exclude_args()/build_parachute_script(), with is_excluded_relpath as the Python mirror for the fake backend; version-pinned via TRANSFER_CONTRACT_VERSION.

### MCP stdio<->HTTP proxy (deliberately stdlib-only)

The MCP proxy region is a well-separated, stdlib-only stdio<->HTTP router: four small files with one-directional dependencies, a genuinely clean transport-vs-domain error taxonomy, and comments that consistently explain WHY rather than restate code. The intentional NIH (no third-party deps) is documented and machine-enforced by test_mcp_server_imports_only_stdlib, and the hand-rolled JSON-RPC/HTTP code is small and correct, so it earns its keep. The main blemish is modularity/brevity: the two-upstream `/mcp/tools` fetch-and-cache loop is triplicated across _list_tools, _plane_for, and _tool_is_project_scoped (each also issuing redundant round-trips), and two of those caches are grown lazily via getattr rather than declared in __init__."}

**Problems**

- **[low · modularity]** Two-upstream /mcp/tools fetch loop triplicated across three methods
  - _Evidence:_ proxy.py:243-256 (_list_tools), 428-444 (_tool_is_project_scoped), 453-467 (_plane_for) each run the identical `for is_cloud,url in ((True,control_url),(False,daemon_url)): if not url: continue; try: GET /mcp/tools except _UpstreamError: continue` skeleton, differing only in what they harvest (full tool, project_id presence, plane).
  - _Fix:_ Extract one `_each_catalog_tool()` generator (or fetch the merged catalog once and derive plane/scoped maps from it) — this is the 3+ repetition that the rubric says warrants a utility, and it also makes three redundant HTTP round-trips to the same endpoint collapsible.
- **[low · modularity]** Caches declared lazily via getattr instead of in __init__
  - _Evidence:_ proxy.py:425 `getattr(self, "_scoped_cache", None)` and 450 `getattr(self, "_plane_cache", None)` grow undeclared instance attributes on first use, while the sibling `self._project_id` is properly initialized in __init__ (line 140).
  - _Fix:_ Initialize `_scoped_cache`/`_plane_cache` to None in __init__ alongside `_project_id` for one consistent, greppable cache lifecycle; drops the getattr indirection.

**Strengths**

- Stdlib-only NIH is deliberate, enforced, and lean — Every import in proxy/__main__/daemon_marker/time_utils is stdlib or intra-package; test_plane_layout.py:876 walks the package and fails on any non-stdlib import so the dual-upstream rewrite can't reach for pydantic/boto3.
- Transport-vs-domain error taxonomy is a clean, narrow seam — proxy.py:194-218 routes _UpstreamError: transport codes in _TRANSPORT_ERROR_CODES (61-69) return as tool RESULTS (isError) so a one-plane outage never disables the server, while domain errors keep the -32000 protocol shape for backward compat.

### Frontend (research_state_ui/src: api/store/feed/pages/mobile/components)

A well-architected ~14k-LOC React frontend that scores high across the rubric: a thin documented api seam, a tiny zustand store with identity-stable selectors, dependency-free util modules, and a self-contained feed feature, with desktop and mobile surfaces genuinely sharing leaf logic (single Feed.jsx, 12 reused components, broadly-reused evidence/graph/experiment helpers) rather than forking it. The one material defect is ~60 lines of synthesis role-resolution policy (role constants, reflectionsByLens, secondaryDocs, version-pinning fallbacks) duplicated verbatim between ProjectSynthesisPanel.jsx and MobileSynthesisScreen.jsx — a kill-duplication trigger made worse because the same risky version-pinning logic must not drift. A secondary, milder issue is the poll-while-visible lifecycle recipe hand-rolled at ~6 component sites when the store already encapsulates it.

**Problems**

- **[medium · modularity]** Synthesis role-resolution logic duplicated verbatim across desktop and mobile
  - _Evidence:_ components/ProjectSynthesisPanel.jsx:27-93 and mobile/MobileSynthesisScreen.jsx:24-93 both define TERMINAL_WAVE, REFLECTION_DOC_ROLES, LENS_DOC_ROLES, PRIMARY_ROLES, DOC_ROLE_META, humanizeRole(), reflectionsByLens(), and secondaryDocs() with near-identical bodies (~60 lines). The version-pinning and role-rename-fallback logic is exactly the kind of policy that must not drift between two copies.
  - _Fix:_ Extract the role constants plus reflectionsByLens/secondaryDocs/humanizeRole into a shared module (e.g. components/synthesis/waveModel.js, mirroring the existing shared mobile/graphModel.jsx). Both surfaces then import the same belief-state logic; only the JSX layout stays per-surface.

**Strengths**

- Right-sized dependency set: commodity capabilities outsourced, nothing heavy — package.json deps: react-markdown + remark-gfm (markdown), @xyflow/react (graph canvas), prism-react-renderer (syntax highlight), zustand (state), react-router-dom. No date/util mega-libs; formatters in utils/format.js are 60 lines hand-rolled.
- Clean api seam and feature isolation with one-directional dependencies — api.js is a thin documented fetch wrapper exporting request/mediaUrl; feed/feedApi.js:6 imports ONLY that shared transport and owns its own endpoints, so core api never depends on the feed. Store selectors (useProjectStore.js:155-178) return frozen EMPTY_OBJ/EMPTY_ARR to satisfy useSyncExternalStore identity stability.
- Comments consistently say WHY, not what — api.js:41-46 explains why feed images need fetch+blob (browser won't attach Bearer to a bare img src); useProjectStore.js:147-154 explains the identity-stability contract; pages/Home.jsx:29-30 'Clamp on list shrink (e.g. an experiment just completed)'. try/catch is concentrated on localStorage and network boot, not scattered defensively.

### Domain policy + ports (Protocols) + contracts tables

The domain/ports/contracts region is the strongest layer of the codebase against this rubric: domain modules are pure policy leaves with AST-enforced minimal imports, the dependency graph is strictly acyclic pointing toward stable abstractions, and the derive-from-one-table idiom (TOOL_CONTRACTS in contracts.py, GATE_TABLE in the two gate tables) eliminates whole classes of drift while keeping things lean. The one rubric tension is over-modularization in ports/: synthesis_writers (3 protocols), review_policy, quota_admission, ResourceAssociationPolicy, and the 4 workflow_readers protocols are single-implementer cycle-breakers rather than the >=2-impl-across-modes seams that earn their keep (unlike ResourceObserver/MgmtKeyStore/MetricsArchive/TaskChannel, which legitimately do).

**Strengths**

- Derive-from-one-table idiom eliminates drift across consumers — contracts.py:682-729 derives TOOL_INPUT_MODELS, PROJECT_SCOPED_TOOL_NAMES, CONTROL/DATA/AGGREGATE_PLANE_TOOL_NAMES and static_tool_catalog() purely from TOOL_CONTRACTS; workflow_gates.py:192-209 and synthesis_gates.py:214-223 derive TRANSITION_GRAPH/REQUIREMENTS from one GATE_TABLE.
- Domain modules are genuine pure-policy leaves — domain/graph_lint.py imports only json; review_returns.py only dataclasses; gate tables only {gates,vocabulary,typing}; test_service_layout.py:703-717 AST-bans any domain import of services/state/dataplane/execution/composition.
- Comments say WHY the code cannot, never restate it — contracts.py:23-29 explains the plane-routing semantics; graph_lint.py:22-24 'a 40-node graph is a log; a 16-node graph is a story'; synthesis_gates.py:11-30 documents the FSM and why gates check envelopes not honesty; vocabulary.py legacy-alias comments.
- Contract capability flags keep transport mode-blind — contracts.py:37-38 hosted_control_skip_final_pull / tenant_scoped_sandbox_lookup are data on ToolContract that http_api.py reads (pinned by test_service_layout.py:866-885) instead of branching on tool-name string literals.

### State stores + data plane

Clean 3-plane split; ports 2-plus impls

### GLOBAL LENS: whole-system dependency graph & separation of concerns

The backend dependency graph is a clean, acyclic seven-layer stack (domain -> ports -> services -> {dataplane, execution, state} -> composition -> transport), verified by a full Tarjan SCC scan: every cross-layer edge points one direction toward stable, narrow leaves (utils, state/store.py with a 14-method Protocol-backed surface, pure contracts.py), and no lower layer reaches up into transport/composition. The intended boundaries are unusually well machine-enforced by AST import-ban lints. The two material weaknesses are over-engineering, not under-separation: several single-implementer reader/writer ports are mandated by lints despite not meeting the project's own >=2-impl-from-different-modes bar (applied inconsistently versus project_overview/reflection_tools), plus one fat transport module (http_api.py, 1748 lines fanning out into 14 services) and a small unguarded data-only cycle (contracts <-> feed_contracts).

**Strengths**

- Strictly one-directional layering verified by full SCC scan — Tarjan SCC over all 143 backend modules: domain/ imports only ..utils (domain/experiment_names.py:7); ports/ imports only ..domain.quota_contract (ports/quota_admission.py:7); no module under domain/ports/services/dataplane/execution/state imports any transport/composition module (http_api, app, *_runtime, tool_handlers) - grep returns empty.
- Stable, narrow hubs absorb the high fan-in — utils (fan-in 55) is a stdlib-only leaf (utils.py imports only datetime/uuid + typing). state/store.py (fan-in 25, 1169 lines) exposes just 14 public methods behind Row/ResultCursor/Connection Protocols (store.py:25-45) and reaches up to nothing. contracts.py is pure (imports only domain.vocabulary).
- Boundary intent is encoded as enforced AST import-ban lints — tests/structure/test_plane_layout.py asserts CONTROL_MODULES import none of {dataplane,sandbox_conn,ssh_rsync,subprocess,workspace} (l.393-403), dataplane never imports services (l.540-545), ports stay neutral, and app import leaves local-IO modules unloaded via subprocess probe (l.839-861).
- Mode-polymorphic ports correctly carry >=2 implementers — SandboxWorker port: LocalDataPlaneWorker (dataplane/worker.py:153) + ControlSandboxWorker (control_runtime.py:262). MgmtKeyStore port: LocalMgmtKeyStore (state/mgmt_keys.py:17) + MountedMgmtKeyStore (state/managed_mgmt_keys.py:17). TaskChannel/MetricsArchive likewise span modes.

### GLOBAL LENS: OSS-reuse / NIH across the whole repo

Against the OSS-reuse/NIH lens this repo is exemplary. Every commodity capability with a good OSS fit is delegated to a maintained, lightly-imported, profile-scoped dependency (FastAPI/uvicorn/httpx/pydantic for the HTTP surface, boto3 for S3 + SigV4/presign, psycopg for Postgres, modal as the lazy provider SDK, and react-markdown/remark-gfm/prism-react-renderer/@xyflow/react/zustand on the frontend). The remaining hand-rolled code falls cleanly into two rubric-sanctioned buckets — deliberate documented stdlib-only NIH (the MCP stdio proxy, the SSRF unfurl guard, env/secret/ssh-key/iso micro-helpers) and genuinely CORE domain logic no library would fit (transcript segmentation, per-VM usage sampler, SQLite-shaped Postgres dialect, figure layout). No heavy dep is pulled for a few-line job.

**Strengths**

- Heavy commodities correctly delegated to OSS and profile-gated so each plane stays slim — research_plugin/pyproject.toml:18-40 (fastapi/httpx/pydantic/uvicorn base; modal/psycopg[binary]/boto3 in control extra; daemon=[]); research_state_ui/package.json:9-18 (react-markdown, remark-gfm, prism-react-renderer, @xyflow/react, zustand, react-router-dom)
- MCP stdio proxy is justified, test-pinned stdlib-only NIH — research_plugin/mcp_server/proxy.py:1-43 (urllib.request only, no FastAPI/httpx); tests/structure/test_plane_layout.py + tests/surface/test_mode_config.py pin the stdlib-only invariant
- SSRF unfurl guard is CORE security, hand-rolled on stdlib by design — research_plugin/backend/services/feed_unfurl.py:1-23,63-155 (ipaddress/socket/urllib/html.parser; per-hop public-IP revalidation, bounded body/redirects, documented TOCTOU limit)
- S3 SigV4/presign and Postgres driver use OSS instead of hand-rolled crypto/SQL — research_plugin/backend/state/s3_blobs.py:51-53,99-138 (boto3 client + generate_presigned_url, gated import); research_plugin/backend/state/dialects.py:140-207 (psycopg + dict_row, lazy gated)
- Postgres dialect is a thin string-translation seam, not a hand-rolled ORM — research_plugin/backend/state/dialects.py:41-99 (placeholder ? -> %s, SCHEMA DDL regex translation, BLOB-growth guard; test_postgres_dialect.py keeps the no-?-in-literals invariant honest)


---

# Improvement Plan (sequenced, executable)

_Generated by a second multi-agent pass: 7 planning agents read the live code to produce exact steps, then a sequencing agent ordered them into dependency-aware phases. Baseline at plan time: **854 backend tests passing, 0 failing**. All refactors are behavior-preserving._

## Load-bearing invariants (must not drift)

BYTE-IDENTICAL INVARIANTS THAT MUST NOT DRIFT (these are load-bearing, compared for equality / parsed positionally elsewhere): (1) review snapshot-id format in domain/review_snapshot.review_snapshot_id — the '|'-join field order [type, id, status, str(attempt_index), ','.join(sorted(resource_tokens))] AND the resource-token f-string \"{id}:{assoc_version_id or version_token}:{role}:{attempt_index}\" must be copied byte-for-byte from experiments.py:719-730; reviews.py:162-163 compares it for equality and reviews.py:559-588 snapshot_from_id parses it back positionally (split('|',4) then per-token rsplit/split). (2) covered_terminal_ids in domain/reflection_policy — adopts experiments.py's stricter isinstance(...,Mapping) filter (dict IS a Mapping, so it is a strict superset of the old dict filter and the old no-filter syntheses path only ever holds dict rows -> result unchanged); the json.loads + except json.JSONDecodeError guard STAYS caller-side in experiments.py (helper takes a pre-parsed Mapping). (3) frontend docVersion = res => res.association_version_id || null is a per-wave version-pinning key — copy verbatim into waveModel.js. (4) ssh_rsync rsync flag list and the only behavioral branch is the push src/dst swap [local,remote] if push else [remote,local]. (5) http_api graph payload tails 700-718 == 795-813 are byte-identical (verified); hoist into one _graph_payload method (stays in transport layer because it touches self.app.graph_refs — NOT a domain leaf). LAYER RULES: shared helpers must live where callers may import them — domain/ is a pure leaf importable by services/ (reflection_policy already is); domain modules take NO Connection (that is why _tenant_for_project goes on SandboxRegistry, not domain/). domain/reflection_projection.py must stay imports=={'typing'} (import-ban test at test_service_layout.py:675). DAEMON STDLIB-ONLY: add no third-party deps anywhere; proxy.py adds only typing.Iterator, reflection_policy.py adds only collections.abc — both stdlib. KNOWN-GREEN BASELINE: there are 2 pre-existing feed test failures unrelated to every item here; the full-suite gate (cd research_plugin && python -m pytest -q) must show ONLY those 2 failing at the end. LINE-NUMBER NOTE (re-verified against current code, branch codex/server-side-split): all cited line numbers are accurate; minor cosmetic deltas — item5 inbound rename in reviews.py is a single line (67) and the two-line outbound blocks have their assignment on 539/695/702. PER-PHASE GATES are the authoritative checkpoints; do not advance a phase until its gate passes.

## Execution phases

### Phase 1 — Load-bearing invariant unification: domain leaf helpers (reflection-drift + snapshot-id)

- Unify reflection-drift terminal-minus-covered into domain/reflection_policy.covered_terminal_ids, called by ExperimentService and SynthesisService.
- Extract one shared review-pinning target_snapshot_id helper (domain/review_snapshot.review_snapshot_id) used by ExperimentService and SynthesisService.

**Files:** `research_plugin/backend/domain/reflection_policy.py`, `research_plugin/backend/domain/review_snapshot.py`, `research_plugin/backend/services/experiments.py`, `research_plugin/backend/services/syntheses.py`, `research_plugin/tests/workflow/test_review_snapshot.py`

**Gate:** `cd research_plugin && python -m pytest tests/workflow/test_synthesis_gates.py tests/workflow/test_review_snapshot.py tests/workflow/ tests/structure/ -q`

### Phase 2 — Frontend wave-model extraction (independent React surface)

- Extract shared synthesis role-resolution model into research_state_ui/src/components/synthesis/waveModel.js, consumed by ProjectSynthesisPanel.jsx (desktop) and MobileSynthesisScreen.jsx (mobile).

**Files:** `research_state_ui/src/components/synthesis/waveModel.js`, `research_state_ui/src/components/ProjectSynthesisPanel.jsx`, `research_state_ui/src/mobile/MobileSynthesisScreen.jsx`

**Gate:** `cd /Users/guraltoo/Documents/dev/proj/experiments/research-suite/research_state_ui && npm run build`

### Phase 3 — Single-file dedupe refactors (ssh_rsync builders + http_api/proxy hoists)

- Collapse ssh_rsync command-builder and run-loop duplication in research_plugin/backend/execution/ssh_rsync.py.
- Hoist duplicated graph payload in http_api.py and unify the dual-upstream catalog-fetch skeleton in mcp_server/proxy.py.

**Files:** `research_plugin/backend/execution/ssh_rsync.py`, `research_plugin/backend/http_api.py`, `research_plugin/mcp_server/proxy.py`, `research_plugin/tests/structure/test_service_layout.py`

**Gate:** `cd research_plugin && python -m pytest tests/sandbox/test_ssh_rsync.py tests/sandbox/test_parachute.py tests/state/test_sync_leases.py tests/structure/test_plane_layout.py tests/structure/test_service_layout.py tests/surface/test_http_api.py tests/surface/test_proxy_split.py tests/surface/test_proxy_mcp.py -q`

### Phase 4 — Naming-boundary centralization (reflection_projection adapter)

- Centralize the internal-synthesis / external-reflection target_type + inbound tool-name/target_type renames through domain/reflection_projection.py.

**Files:** `research_plugin/backend/domain/reflection_projection.py`, `research_plugin/backend/services/reviews.py`, `research_plugin/backend/services/workflow.py`, `research_plugin/backend/services/resources.py`, `research_plugin/backend/services/reflection_tools.py`, `research_plugin/tests/workflow/test_reflection_projection.py`

**Gate:** `cd research_plugin && python -m pytest tests/workflow/test_reflection_projection.py tests/workflow/test_synthesis_gates.py tests/structure/test_service_layout.py tests/structure/test_plane_layout.py tests/surface/test_http_api.py tests/state/test_resource_versions.py -q`

### Phase 5 — Dead-code and compat-shim deletions (depends on prior moves)

- Delete dead code and test-only compat shims: pinned.pinned_version_row, resources._get_version, sandboxes._pulled_mlflow_db_path, dedupe _tenant_for_project into SandboxRegistry.tenant_for_project, delete services/reflection_policy.py shim, delete execution/errors.py + execution/types.py shims.

**Files:** `research_plugin/backend/services/sandbox_registry.py`, `research_plugin/backend/services/sandboxes.py`, `research_plugin/backend/services/sandbox_provisioner.py`, `research_plugin/backend/services/pinned.py`, `research_plugin/backend/services/resources.py`, `research_plugin/backend/services/reflection_policy.py`, `research_plugin/backend/execution/errors.py`, `research_plugin/backend/execution/types.py`, `research_plugin/tests/sandbox/test_sandbox_service.py`, `research_plugin/tests/sandbox/test_modal_sandbox_backend.py`, `research_plugin/tests/sandbox/test_cleanup.py`, `research_plugin/tests/sandbox/test_control_reaper_recovery.py`, `research_plugin/tests/sandbox/test_lambda_availability.py`, `research_plugin/tests/sandbox/test_chaos.py`, `research_plugin/tests/sandbox/test_router_restart.py`, `research_plugin/tests/structure/test_service_layout.py`

**Gate:** `cd research_plugin && python -m pytest tests/sandbox/ tests/structure/test_service_layout.py tests/structure/test_plane_layout.py tests/workflow/test_synthesis_gates.py tests/surface/test_mode_config.py -q`

### Phase 6 — Full-suite behavior-preservation backstop

- Run the entire backend test suite as the final behavior-preservation gate; confirm only the 2 known pre-existing feed failures remain.
- Run the vite build once more to confirm the frontend extraction is clean.

**Files:** 

**Gate:** `cd research_plugin && python -m pytest -q && cd ../research_state_ui && npm run build`

## Per-item steps

### Unify reflection-drift "terminal-minus-covered" computation into a shared domain helper in domain/reflection_policy.py, called by both ExperimentService._reject_reflection_blocked_experiment_create and SynthesisService.reflection_signal.

_Current state:_ Two copies of the same "drift = current terminal experiments minus those covered by the last published reflection wave's corpus" logic exist, both keyed off the published wave's corpus `terminal_experiments` list.

(A) research_plugin/backend/services/experiments.py:218-251 — `_terminal_experiments_since_last_reflection(self, *, conn, project_id)` returns `tuple[int, str|None]`. It: builds `current_terminal` as a set of ids via SQL over `TERMINAL_STATUSES` (lines 221-231); SELECTs the latest published synthesis's `id, corpus_json` (232-239); if none, returns `(len(current_terminal), None)` (240-241); else `json.loads(corpus_json or "{}")` guarded by `except json.JSONDecodeError: corpus = {}` (242-245); derives `covered = {str(exp.get("id")) for exp in (corpus.get("terminal_experiments") or []) if isinstance(exp, dict)}` (246-250); returns `(len(current_terminal - covered), str(published["id"]))` (251). Sole caller is `_reject_reflection_blocked_experiment_create` at experiments.py:135-170 (call site line 136-138), which compares `debt` to `REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD`.

(B) research_plugin/backend/services/syntheses.py:1277-1358 — `reflection_signal(self, *, project_id, conn=None)`. It builds `current_terminal` as a dict id->status via SQL over `EXPERIMENT_TERMINAL_STATUSES` (1290-1299, the dict is reused for nothing terminal-side but mirrors the claims dict pattern), fetches `published = self.latest_published(...)` which returns a hydrated dict whose `corpus` is ALREADY json-parsed (syntheses.py get_state line 256), then at 1313-1322 derives `covered_ids = {str(exp.get("id")) for exp in corpus.get("terminal_experiments", [])}` (NOTE: no `isinstance(exp, dict)` filter here, and no JSONDecodeError guard because corpus is pre-parsed), and at 1324 computes `new_terminal = sorted(set(current_terminal) - covered_ids)`. Downstream it uses `len(new_terminal)`, `len(covered_ids & set(current_terminal))` (1342-1343), and threshold comparisons.

Both terminal-status constants are the SAME object: domain/workflow_gates.py:39 `TERMINAL_STATUSES = EXPERIMENT_TERMINAL_STATUSES` (= frozenset {complete, failed, abandoned}, vocabulary.py:28), so the terminal sets are byte-identical across callers. The genuinely duplicated, load-bearing piece is the corpus->covered-id extraction; the `experiment_create_blocked` equality vs the BLOCK threshold is the same in both (experiments.py:139 `debt >= ...` indirectly; syntheses.py:1333-1335) and MUST stay equivalent so the Home badge, the soft nudge, and the hard experiment.create block agree.

The shared layer is already legal: both services import from `..domain.reflection_policy` today (experiments.py:12, syntheses.py:22-25) for the two threshold constants, and domain/reflection_policy.py is a pure leaf (currently no imports). domain/ is importable by services/ per the layer rules.

_New shared code:_ New helper in research_plugin/backend/domain/reflection_policy.py (pure domain leaf; add `from collections.abc import Iterable, Mapping` at top — stdlib only, allowed: domain/graph_lint.py already imports json and the DOMAIN_FORBIDDEN_SEGMENTS test only bans backend-layer segments).

```python
def covered_terminal_ids(corpus: Mapping[str, object] | None) -> set[str]:
    """Ids of terminal experiments a published reflection corpus already covers.

    Single source of truth for reflection-drift: callers diff this against the
    project's current terminal experiments. Tolerates a missing/empty corpus
    and non-dict list entries so a malformed snapshot never raises here."""
    if not corpus:
        return set()
    entries = corpus.get("terminal_experiments") or []
    return {
        str(exp.get("id"))
        for exp in entries
        if isinstance(exp, Mapping)
    }


def terminal_drift_count(
    *, current_terminal_ids: Iterable[str], corpus: Mapping[str, object] | None
) -> int:
    """Count of current terminal experiments not yet covered by the corpus."""
    return len(set(current_terminal_ids) - covered_terminal_ids(corpus))
```

Notes on byte-identity / behavior preservation: `covered_terminal_ids` adopts experiments.py's stricter form (`isinstance(... Mapping)` filter — `dict` IS a `Mapping`, so it is a strict superset of the old `isinstance(exp, dict)` and still accepts every previously-accepted entry; syntheses.py previously had NO filter, but its corpus only ever contains dict rows from `_corpus_snapshot` rows_to_dicts at syntheses.py:230, so filtering non-Mapping entries cannot drop a real covered id => unchanged result). The JSONDecodeError concern stays in experiments.py's caller (it parses raw `corpus_json`); the helper takes an already-parsed Mapping, so the parse+guard remains caller-side and the helper is parse-agnostic. syntheses.py keeps computing `covered_ids` via the helper and reuses it for both `new_terminal` and the `covered & current` intersection.

_Steps:_

1. `research_plugin/backend/domain/reflection_policy.py` (lines 1-8 (whole file; anchor: "REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD = 5")) — Add `from collections.abc import Iterable, Mapping` near the top (after the module docstring, before the threshold constants). Append the two new functions `covered_terminal_ids` and `terminal_drift_count` exactly as sketched in sharedCode after the existing threshold constants.
2. `research_plugin/backend/services/experiments.py` (import line 12 (anchor: "from ..domain.reflection_policy import REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD")) — Extend the import to also bring in the helper: `from ..domain.reflection_policy import (REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD, covered_terminal_ids)`. (Keep importing the threshold constant.)
3. `research_plugin/backend/services/experiments.py` (lines 242-251 inside `_terminal_experiments_since_last_reflection` (anchor: "corpus = json.loads(str(published[\"corpus_json\"] or \"{}\"))")) — Replace the inline `covered = {...}` set-comprehension (lines 246-250) with `covered = covered_terminal_ids(corpus)`. KEEP the json.loads + `except json.JSONDecodeError: corpus = {}` block (242-245) and the final `return len(current_terminal - covered), str(published["id"])` (251) byte-identical. Net effect: 5 comprehension lines -> 1 call line.
4. `research_plugin/backend/services/syntheses.py` (import lines 22-25 (anchor: "from ..domain.reflection_policy import (")) — Add `covered_terminal_ids` to the existing reflection_policy import tuple alongside the two threshold constants.
5. `research_plugin/backend/services/syntheses.py` (lines 1310-1324 inside `reflection_signal` (anchor: "corpus = published.get(\"corpus\") or {}")) — Replace the `if published is None: covered_ids: set[str] = set() ... else: corpus = published.get("corpus") or {}; covered_ids = {str(exp.get("id")) for exp in corpus.get("terminal_experiments", [])}; snapshot_claims = {...}` block so that `covered_ids` is computed via `covered_terminal_ids(published.get("corpus") if published else None)`. KEEP the `snapshot_claims` derivation and the `published is None` guard for `snapshot_claims` exactly as-is (snapshot_claims is unrelated to this item — do NOT fold claims). Concretely: set `covered_ids = covered_terminal_ids(None if published is None else (published.get("corpus") or {}))` and leave `snapshot_claims` computed in the existing if/else. Line 1324 `new_terminal = sorted(set(current_terminal) - covered_ids)` stays unchanged.

_Verify:_ cd research_plugin && python -m pytest tests/workflow/test_synthesis_gates.py -q   # covers reflection_signal (covered_terminal_experiments / new_terminal_since_publish / experiment_create_blocked at lines ~1161-1289) AND the hard experiment.create block at lines 1372-1392 — both code paths; must stay green unchanged

_Risk:_ low · _LOC delta:_ -2

### Extract one shared target_snapshot_id helper used by ExperimentService and SynthesisService (the review-pinning equality key)

_Current state:_ Two byte-identical (modulo the "experiment"/"synthesis" literal and the local var name) snapshot-id builders exist:
- research_plugin/backend/services/experiments.py:717-731 `_target_snapshot_id(self, *, conn, experiment_id)` — fetches `self.get_state(...)`, builds `resource_tokens` as `f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role','')}:{res.get('association_attempt_index',0)}"`, then `"|".join(["experiment", id, status, str(attempt_index), ",".join(sorted(resource_tokens))])`. Public wrapper at experiments.py:714-715 `target_snapshot_id(...)`.
- research_plugin/backend/services/syntheses.py:1204-1218 — identical body with literal "synthesis" and local var `synthesis`. Public wrapper at syntheses.py:1201-1202.
Verified byte-identical via normalized diff (only the literal + var name differ).

Load-bearing in three ways, so unifying is the SAFE move:
1. Equality compare at research_plugin/backend/services/reviews.py:162-163 (`snapshot_now != req["target_snapshot_id"]`).
2. Stored as the pinning key in review_requests/reviews/decisions (reviews.py:86,123; store schema state/store.py:198,228,554).
3. Parsed back positionally by reviews.py `snapshot_from_id` at reviews.py:559-588 (`parts = snapshot_id.split("|",4)`; field order target_type|id|status|attempt|tokens; each token re-parsed via rsplit(":",2)+split(":",1)). The `|`-join field order AND the resource-token format MUST stay byte-identical.

reviews.py:631-640 `_target_snapshot_id` dispatches by target_type to `self.experiments.target_snapshot_id` / `self.syntheses.target_snapshot_id` — these PUBLIC methods must remain (asserted by structure test).

Layer: research_plugin/backend/domain/ exists and is the pure leaf importable by services/ (services already do `from ..domain.X import Y`). domain is pure (no DB), so the helper takes the already-fetched target dict (NOT a conn); each service keeps a thin wrapper that fetches state and delegates. Daemon stdlib-only constraint satisfied (pure string ops, no new deps).

_New shared code:_ New file research_plugin/backend/domain/review_snapshot.py (pure leaf, stdlib-only):

```
"""The review-pinning snapshot id: a byte-stable equality key.

Compared for equality in services/reviews.py and parsed back by
snapshot_from_id, so this format is load-bearing and must not drift.
"""
from __future__ import annotations
from typing import Any

def review_snapshot_id(*, target_type: str, target: dict[str, Any]) -> str:
    """`type|id|status|attempt|sorted-comma-joined-resource-tokens`.

    `target` is a get_state() dict with id/status/attempt_index and
    current_attempt_resources. Field order and token format are an
    equality key — keep byte-identical."""
    resource_tokens = [
        f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role', '')}:{res.get('association_attempt_index', 0)}"
        for res in target.get("current_attempt_resources", [])
    ]
    return "|".join(
        [
            target_type,
            target["id"],
            target["status"],
            str(target["attempt_index"]),
            ",".join(sorted(resource_tokens)),
        ]
    )
```

Each service's private `_target_snapshot_id` collapses to a 2-line wrapper; the public `target_snapshot_id` wrappers (experiments.py:714-715, syntheses.py:1201-1202) are UNCHANGED so the structure-test surface is preserved.

_Steps:_

1. `research_plugin/backend/domain/review_snapshot.py` (NEW FILE (does not exist yet; sibling of existing domain/synthesis_gates.py, domain/review_gates.py)) — Create the file with the `review_snapshot_id(*, target_type, target)` function exactly as sketched in sharedCode. Copy the resource_tokens f-string and the `"|".join([...])` list BYTE-FOR-BYTE from experiments.py:719-730 (parameterizing only the literal as `target_type` and renaming the local `experiment`->`target`). Do not reformat the f-string or reorder the join list.
2. `research_plugin/backend/services/experiments.py` (import block, lines 8-13 (anchor: `from ..domain.workflow_gates import (`). Insert alphabetically near the other domain imports.) — Add `from ..domain.review_snapshot import review_snapshot_id`.
3. `research_plugin/backend/services/experiments.py` (lines 717-731 (anchor: `def _target_snapshot_id(self, *, conn, experiment_id: str) -> str:`)) — Replace the method BODY (lines 718-731, the get_state + resource_tokens comprehension + the `"|".join([...])`) with two lines: `experiment = self.get_state(experiment_id=experiment_id, conn=conn)` then `return review_snapshot_id(target_type="experiment", target=experiment)`. Keep the `def` signature and the public wrapper at 714-715 untouched.
4. `research_plugin/backend/services/syntheses.py` (import block, lines 19-35 (anchor: `from ..domain.synthesis_gates import (`). Insert near the other domain imports.) — Add `from ..domain.review_snapshot import review_snapshot_id`.
5. `research_plugin/backend/services/syntheses.py` (lines 1204-1218 (anchor: `def _target_snapshot_id(self, *, conn, synthesis_id: str) -> str:`)) — Replace the method BODY (lines 1205-1218) with two lines: `synthesis = self.get_state(synthesis_id=synthesis_id, conn=conn)` then `return review_snapshot_id(target_type="synthesis", target=synthesis)`. Keep the `def` signature and the public wrapper at 1201-1202 untouched.
6. `research_plugin/tests/workflow/test_review_snapshot.py` (NEW FILE (optional but recommended; pattern: tests/workflow/test_synthesis_gates.py imports `from backend.domain....`)) — Add a unittest.TestCase importing `from backend.domain.review_snapshot import review_snapshot_id` that locks the format: field order, sorted tokens, association_version_id-wins-over-version_token (and fallback), and empty-resources trailing segment.

_Tests:_ No existing assertion must CHANGE — behavior is byte-preserved (format string unchanged). research_plugin/tests/structure/test_service_layout.py::ServiceLayoutTest::test_review_service_uses_direct_concrete_targets (lines 607-648) still passes: the public `target_snapshot_id` methods are retained and reviews.py still calls them (asserted at test lines 629 and 635).; ADD research_plugin/tests/workflow/test_review_snapshot.py (step 6): unit test for backend.domain.review_snapshot.review_snapshot_id locking the load-bearing format — field order, token sort, association_version_id precedence/fallback, empty-resources case.

_Verify:_ cd research_plugin && diff <(sed -n '719,731p' backend/services/experiments.py | sed 's/experiment/X/g') <(sed -n '1206,1218p' backend/services/syntheses.py | sed 's/synthesis/X/g')  # PRE-change: empty diff proves bodies identical modulo literal (already verified)

_Risk:_ low · _LOC delta:_ -4

### Extract shared synthesis role-resolution model into research_state_ui/src/components/synthesis/waveModel.js, consumed by both ProjectSynthesisPanel.jsx (desktop) and MobileSynthesisScreen.jsx (mobile).

_Current state:_ Two surfaces duplicate the same wave role-resolution policy + belief-state logic verbatim (only comments and unrelated date formatters differ). DESKTOP research_state_ui/src/components/ProjectSynthesisPanel.jsx: TERMINAL_WAVE (L27), REFLECTION_DOC_ROLES (L34), LENS_DOC_ROLES (L35), PRIMARY_ROLES (L36), DOC_ROLE_META (L40-43), humanizeRole (L45-47), reflectionsByLens (L61-77), secondaryDocs (L82-93); plus inline-in-component the reflectionDoc resolution (L178-180) and the docVersion version-pinning fallback with its 4-line comment (L181-185). MOBILE research_state_ui/src/mobile/MobileSynthesisScreen.jsx: TERMINAL_WAVE (L24), REFLECTION_DOC_ROLES (L29), LENS_DOC_ROLES (L30), PRIMARY_ROLES (L31), DOC_ROLE_META (L35-38), humanizeRole (L47-49), reflectionsByLens (L62-78), secondaryDocs (L82-93); inline reflectionDoc resolution (L157-159) and docVersion fallback + comment (L160-164). Verified byte-identical bodies via diff: reflectionsByLens, secondaryDocs, REFLECTION_DOC_ROLES, LENS_DOC_ROLES, DOC_ROLE_META, humanizeRole, the reflectionDoc block, and the docVersion arrow are identical; PRIMARY_ROLES differs only in spread order (Set membership is identical, safe). docVersion uses res.association_version_id — a load-bearing per-wave version-pinning key compared elsewhere — so unifying byte-identically is the SAFE move. Convention to mirror: research_state_ui/src/mobile/graphModel.jsx exports pure helpers consumed across surfaces (importers: MobileGraphSection.jsx:5, MobileSynthesisScreen.jsx:8). LAYER CHECK: AST import-ban tests in research_plugin/tests/structure/ are Python-only and govern the daemon/services Python layers; test_deploy_artifacts.py:107 explicitly excludes research_state_ui/ from the build context, so they do NOT constrain this React move. No vitest/eslint configured in research_state_ui (package.json has only dev/build/preview).

_New shared code:_ New file research_state_ui/src/components/synthesis/waveModel.js (plain-JS pure-helper module, mirrors mobile/graphModel.jsx convention; NO JSX so .js not .jsx; sits in components/synthesis/ alongside WaveSelector.jsx and LensReflectionCard.jsx which the desktop panel already imports, and is equally reachable from src/mobile/). EXPORTED: `export const TERMINAL_WAVE = new Set(['published', 'abandoned']);`  `export const REFLECTION_DOC_ROLES = ['reflection_doc', 'synthesis_doc'];`  `export const LENS_DOC_ROLES = ['reflection_lens_doc', 'reflection'];`  `export function reflectionsByLens(wave){...}` (copy desktop L61-77 body verbatim);  `export function secondaryDocs(resources){...}` (copy desktop L82-93 body verbatim);  `export function resolveReflectionDoc(resources){ return REFLECTION_DOC_ROLES.map(role => resources.find(r => r.association_role === role)).find(Boolean) || null; }` (captures the inline reflectionDoc block);  `export const docVersion = res => res.association_version_id || null;` (carry over the 4-line pinning comment above it). PRIVATE (module-scope, not exported — only used internally): `const PRIMARY_ROLES = new Set(['graph', ...REFLECTION_DOC_ROLES, ...LENS_DOC_ROLES]);`  `const DOC_ROLE_META = {...};` (desktop L40-43 verbatim);  `function humanizeRole(role){...}` (desktop L45-47 verbatim). NOT moved (per-surface): shortDateTime (desktop-only), shortDate + WAVE_DOT (mobile-only).

_Steps:_

1. `research_state_ui/src/components/synthesis/waveModel.js` (new file) — Create the shared module exactly as sketched in sharedCode: TERMINAL_WAVE, REFLECTION_DOC_ROLES, LENS_DOC_ROLES exported; private PRIMARY_ROLES, DOC_ROLE_META, humanizeRole; exported reflectionsByLens, secondaryDocs, resolveReflectionDoc, docVersion. Copy bodies byte-for-byte from ProjectSynthesisPanel.jsx (the source of truth) to preserve the version-pinning invariant. Keep one-liner comments per code-quality rubric.
2. `research_state_ui/src/components/ProjectSynthesisPanel.jsx` (L7-8 anchor 'import WaveSelector from ./synthesis/WaveSelector;') — Add import: `import { TERMINAL_WAVE, reflectionsByLens, secondaryDocs, resolveReflectionDoc, docVersion } from './synthesis/waveModel';` next to the existing synthesis/ imports.
3. `research_state_ui/src/components/ProjectSynthesisPanel.jsx` (L27-47 anchor 'const TERMINAL_WAVE = new Set' through end of humanizeRole; L61-93 anchor 'function reflectionsByLens(wave)' and 'function secondaryDocs(resources)') — DELETE the module-scope declarations now provided by waveModel: TERMINAL_WAVE (L27), the REFLECTION_DOC_ROLES/LENS_DOC_ROLES/PRIMARY_ROLES block + its comments (L29-36), DOC_ROLE_META + comment (L38-43), humanizeRole (L45-47), reflectionsByLens + comment (L58-77), secondaryDocs + comment (L79-93). KEEP shortDateTime (L49-56) and the Collapsible component (L96-111) — both stay per-surface.
4. `research_state_ui/src/components/ProjectSynthesisPanel.jsx` (L176-185 anchor 'const reflectionDoc = REFLECTION_DOC_ROLES') — Replace the inline reflectionDoc resolution (L178-180) with `const reflectionDoc = resolveReflectionDoc(waveResources);` and DELETE the local `const docVersion = res => ...` (L181-185, now imported). Keep the 4-line pinning comment OR rely on the one carried into waveModel — do not leave it orphaned. reflectionsByLens/secondaryDocs call sites (L172, L294) are unchanged (now resolve to imports).
5. `research_state_ui/src/mobile/MobileSynthesisScreen.jsx` (L8 anchor "import { normalizeLogic, makeLogicDetail } from './graphModel';") — Add import: `import { TERMINAL_WAVE, reflectionsByLens, secondaryDocs, resolveReflectionDoc, docVersion } from '../components/synthesis/waveModel';`.
6. `research_state_ui/src/mobile/MobileSynthesisScreen.jsx` (L24-49 anchor 'const TERMINAL_WAVE = new Set' through humanizeRole; L62-93 anchor 'function reflectionsByLens(wave)' and 'function secondaryDocs(resources)') — DELETE module-scope dups now imported: TERMINAL_WAVE (L24), REFLECTION_DOC_ROLES/LENS_DOC_ROLES/PRIMARY_ROLES + comments (L26-31), DOC_ROLE_META + comment (L33-38), humanizeRole + nothing-else (L47-49), reflectionsByLens + comment (L58-78), secondaryDocs + comment (L80-93). KEEP WAVE_DOT (L40-45) and shortDate (L51-56) — mobile-only.
7. `research_state_ui/src/mobile/MobileSynthesisScreen.jsx` (L157-165 anchor 'const reflectionDoc = REFLECTION_DOC_ROLES') — Replace inline reflectionDoc resolution (L157-159) with `const reflectionDoc = resolveReflectionDoc(waveResources);` and DELETE local `const docVersion = res => ...` (L160-164, now imported). secondaryDocs call (L165) and reflectionsByLens call (L154) now resolve to imports; isOpen check at L121 uses the imported TERMINAL_WAVE.

_Verify:_ cd /Users/guraltoo/Documents/dev/proj/experiments/research-suite/research_state_ui && grep -rn "function reflectionsByLens\|function secondaryDocs\|function humanizeRole\|const DOC_ROLE_META\|const PRIMARY_ROLES\|const TERMINAL_WAVE" src/components/ProjectSynthesisPanel.jsx src/mobile/MobileSynthesisScreen.jsx  # expect ZERO matches after extraction (all live only in waveModel.js)

_Risk:_ low · _LOC delta:_ -55

### Collapse ssh_rsync command-builder and run-loop duplication in research_plugin/backend/execution/ssh_rsync.py

_Current state:_ All cited code is in /Users/guraltoo/Documents/dev/proj/experiments/research-suite/research_plugin/backend/execution/ssh_rsync.py (line numbers verified against current file, NOT drifted from the scan).

DUPLICATION 1 — command builders (byte-identical except the final src/dst pair ordering):
- _pull_command: lines 337-372. Body: local_dir.mkdir (349); ssh string (350-354); command list with rsync flags (355-365); exclude loop (366-367); command.extend([REMOTE, LOCAL]) where remote-then-local order is the ONLY difference (368-371: `f"{ssh_user}@{ssh_host}:{remote_dir.rstrip('/')}/"` then `os.fspath(local_dir) + "/"`); return (372).
- _push_command: lines 374-409. Identical mkdir (386), ssh string (387-391), command list (392-402), exclude loop (403-404), then command.extend([LOCAL, REMOTE]) reversed (405-408: `os.fspath(local_dir) + "/"` then `f"{ssh_user}@{ssh_host}:{remote_dir.rstrip('/')}/"`); return (409). Lines 350-367 and 387-404 are byte-for-byte identical.

DUPLICATION 2 — run-loop / result assembly (~95% identical):
- sync(): lines 164-260. Guards (176-182), _ensure_rsync_usable (183), mkdir (184), start clock (185), commands list built via _pull_command (186-233 — pull-specific: 3 conditional passes incl. optional sessions pass), then the SHARED tail: stdout/stderr accumulators + counter init (234-237), run-loop with exit-23 tolerance for optional passes (238-250), SshRsyncResult assembly (251-260, direction="pull").
- push_initial(): lines 262-335. Same guards (272-278), _ensure_rsync_usable (279), mkdir (280), start (281), commands list via _push_command (282-310 — push-specific: 2 passes, extra SESSIONS_DIR_EXCLUDE on main pass), then SHARED tail: accumulators (311-314), identical run-loop (315-325), SshRsyncResult assembly (326-335, direction="push"). The run-loop bodies (238-250 vs 315-325) and result-assembly (251-260 vs 326-335) differ only by the local variable name (pulled vs pushed) and the literal direction string.

Callers: grep confirms ZERO external callers of _pull_command/_push_command/_rsync_command/_run_passes — both private builders are referenced only inside sync()/push_initial() in this file. Public API (sync, push_initial, SshRsyncResult, RsyncBinary, resolve_rsync, SshRsyncSyncer) is unchanged. Module imports are all stdlib (functools, os, re, shlex, shutil, subprocess, time) plus two intra-package sibling imports — daemon stdlib-only constraint preserved by adding no imports.

_New shared code:_ No new file/module — both new helpers stay as private methods inside class SshRsyncSyncer in the SAME module (research_plugin/backend/execution/ssh_rsync.py). This trivially satisfies the layer rule: the only consumers (sync, push_initial) live in the same class, and structure/test_plane_layout.py only forbids OTHER layers from importing execution.ssh_rsync — that ban is unaffected since no import or public symbol changes.

HELPER 1 — unify the two command builders (replaces _pull_command + _push_command):

    def _rsync_command(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: Path,
        remote_dir: str,
        local_dir: Path,
        max_size: str,
        excludes: tuple[str, ...],
        push: bool,
    ) -> list[str]:
        local_dir.mkdir(parents=True, exist_ok=True)
        ssh = (
            f"ssh -i {shlex.quote(os.fspath(key_path))} -p {ssh_port} -o BatchMode=yes "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=10"
        )
        command = [
            resolve_rsync().path,
            "-az", "--delete", "--prune-empty-dirs",
            "--itemize-changes", "--out-format=%n",
            f"--max-size={max_size}", "-e", ssh,
        ]
        for pattern in excludes:
            command.extend(["--exclude", pattern])
        remote = f"{ssh_user}@{ssh_host}:{remote_dir.rstrip('/')}/"
        local = os.fspath(local_dir) + "/"
        command.extend([local, remote] if push else [remote, local])
        return command

(NOTE: keep the rsync flag list spelled byte-identically to the current 355-365 / 392-402 block — those flags are the load-bearing transfer contract. The src/dst ordering swap `[local, remote] if push else [remote, local]` is the ONLY behavioral branch.)

HELPER 2 — extract the shared run-loop + result assembly (private):

    def _run_passes(
        self,
        commands: list[tuple[list[str], bool]],
        *,
        remote_sync_dir: str,
        local_sync_dir: Path,
        direction: str,
    ) -> SshRsyncResult:
        start = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        changed = 0
        ran = 0
        for command, optional in commands:
            result = self.runner(command)
            ran += 1
            stdout_parts.append(result.stdout or "")
            stderr_parts.append(result.stderr or "")
            if result.returncode != 0:
                # rsync 23 == a source path is missing; tolerate only for optional passes.
                if not optional or result.returncode != 23:
                    raise RuntimeError(
                        f"rsync failed with exit {result.returncode}: {(result.stderr or '').strip()}"
                    )
            changed += _count_changed(result.stdout or "")
        return SshRsyncResult(
            pulled=changed,
            duration_seconds=time.monotonic() - start,
            local_dir=str(local_sync_dir),
            remote_dir=remote_sync_dir,
            command_count=ran,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            direction=direction,
        )

Decision: move `start = time.monotonic()` INTO _run_passes (currently set at 185/281 just before the commands list is built). Building the command list is pure string assembly with no I/O, so timing it vs not is behaviorally irrelevant to any assertion — duration_seconds is rounded to 3 decimals and never compared for equality in tests. This keeps the timer with the loop and removes the last bit of tail duplication. (If you prefer maximal caution, instead keep `start` in each caller and pass `start=start` into _run_passes — adds one param, one line per caller, zero behavior change either way.)

_Steps:_

1. `research_plugin/backend/execution/ssh_rsync.py` (lines 337-409 (the two methods `def _pull_command(` at 337 and `def _push_command(` at 374, through their respective `return command` at 372 and 409)) — Replace BOTH _pull_command and _push_command with the single _rsync_command(*, ..., push: bool) method (see sharedCode HELPER 1). Keep the rsync flag list at 355-365/392-402 byte-identical; the only logic change is `command.extend([local, remote] if push else [remote, local])`.
2. `research_plugin/backend/execution/ssh_rsync.py` (Insert immediately after the new _rsync_command (i.e. just before `@staticmethod` / `def _run` currently at 411-413, anchor `def _run(command: list[str]) -> subprocess.CompletedProcess`)) — Add the private _run_passes(commands, *, remote_sync_dir, local_sync_dir, direction) helper (see sharedCode HELPER 2), moving the start-clock, accumulator init, exit-23-tolerant run-loop, and SshRsyncResult assembly into it.
3. `research_plugin/backend/execution/ssh_rsync.py` (Inside sync(): the call sites at 188, 201, 221 use `self._pull_command(` (anchor `self._pull_command(`)) — Rename all three `self._pull_command(` calls to `self._rsync_command(` and append `push=False,` to each keyword-arg call.
4. `research_plugin/backend/execution/ssh_rsync.py` (Inside push_initial(): the call sites at 284 and 298 use `self._push_command(` (anchor `self._push_command(`)) — Rename both `self._push_command(` calls to `self._rsync_command(` and append `push=True,` to each keyword-arg call.
5. `research_plugin/backend/execution/ssh_rsync.py` (sync() shared tail: lines 185 (`start = time.monotonic()`) and 234-260 (accumulators + run-loop + return SshRsyncResult, anchor `stdout_parts: list[str] = []`)) — Delete the start-clock line (185), the accumulator/counter init (234-237), the run-loop (238-250), and the SshRsyncResult assembly (251-260). Replace with: `return self._run_passes(commands, remote_sync_dir=remote_sync_dir, local_sync_dir=local_sync_dir, direction="pull")`.
6. `research_plugin/backend/execution/ssh_rsync.py` (push_initial() shared tail: line 281 (`start = time.monotonic()`) and 311-335 (accumulators + run-loop + return SshRsyncResult, anchor `pushed = 0`)) — Delete the start-clock line (281), the accumulator/counter init (311-314), the run-loop (315-325), and the SshRsyncResult assembly (326-335). Replace with: `return self._run_passes(commands, remote_sync_dir=remote_sync_dir, local_sync_dir=local_sync_dir, direction="push")`.

_Verify:_ cd /Users/guraltoo/Documents/dev/proj/experiments/research-suite/research_plugin && python -m pytest tests/sandbox/test_ssh_rsync.py -q  # all 10 tests must pass unchanged — they drive only public sync()/push_initial() and inspect built command lists via injected runner

_Risk:_ low · _LOC delta:_ -75

### Centralize the internal-synthesis / external-reflection naming boundary by routing the target_type half (and the inbound tool-name/target_type renames) through the existing domain/reflection_projection.py adapter, alongside the status/transition half it already owns.

_Current state:_ domain/reflection_projection.py is a typing-only domain leaf (import-banned to {"typing"} by tests/structure/test_service_layout.py:677-679). It currently owns only the status/transition half: external_reflection_status (lines 8-9), external_reflection_transition (12-29), external_reflection_state (32-41). The target_type rename is hand-patched at the transport edge of three services.

OUTBOUND (internal "synthesis" -> external "reflection") — 8 sites, all behavior-equivalent ternaries/assignments:
- reviews.py:198 (start, in returned dict): `"reflection" if req["target_type"] == "synthesis" else req["target_type"]`
- reviews.py:538-539 (_with_snapshot): `if data.get("target_type") == "synthesis": data["target_type"] = "reflection"`
- reviews.py:552 (reviewer_handoff dict): `"reflection" if target_type == "synthesis" else target_type`
- reviews.py:694-695 (_hydrate_request): `if data.get("target_type") == "synthesis": data["target_type"] = "reflection"`
- reviews.py:701-702 (_hydrate_review): same two-line form
- workflow.py:460 (_review_gate dict): `"reflection" if target_type == "synthesis" else target_type`
- resources.py:632-633 (_hydrate_resource assoc loop): `if assoc.get("target_type") == "synthesis": assoc["target_type"] = "reflection"`
- resources.py:930-931 (_hydrate_version assoc loop): same two-line form
- (workflow.py:374-375 is a RELATED but distinct rename: gate "synthesis_review" -> "reflection_review", which is already the domain's external_reflection_status concern, not target_type.)

INBOUND (external "reflection" -> internal "synthesis") — 3 sites:
- reviews.py:66-68 (request): `target_type = "synthesis" if external_target_type == "reflection" else external_target_type`
- reviews.py:367 (status): `target_type = "synthesis" if target_type == "reflection" else target_type`
- reflection_tools.py:53-57 (transition): hardcodes internal_transition = "submit_synthesis" if transition == "submit_reflection_artifacts" else transition

Grep confirms these are the ONLY rename sites in non-test backend code (reviews.py, workflow.py, resources.py, reflection_tools.py). Layer rule: services/ may import domain/ (callers already import domain.reflection_projection per test line 683). Baseline green: tests/workflow/test_synthesis_gates.py, tests/workflow/test_reflection_projection.py, tests/structure/test_service_layout.py, tests/structure/test_plane_layout.py = 143 passed.

_New shared code:_ Add four tiny pure helpers to backend/domain/reflection_projection.py (stays typing-only; do NOT import anything new — the import-ban test pins imports to {"typing"}). Constants + funcs:

    _INTERNAL_SYNTHESIS = "synthesis"
    _EXTERNAL_REFLECTION = "reflection"

    def external_reflection_target_type(target_type: Any) -> Any:
        """Internal 'synthesis' -> external 'reflection'; pass through all else."""
        return _EXTERNAL_REFLECTION if target_type == _INTERNAL_SYNTHESIS else target_type

    def internal_synthesis_target_type(target_type: Any) -> Any:
        """External 'reflection' -> internal 'synthesis'; pass through all else."""
        return _INTERNAL_SYNTHESIS if target_type == _EXTERNAL_REFLECTION else target_type

    def with_external_target_type(item: dict[str, Any]) -> dict[str, Any]:
        """Return a copy with its 'target_type' field externalized (mutating-copy form for hydrated rows/dicts)."""
        if not isinstance(item, dict) or "target_type" not in item:
            return item
        output = dict(item)
        output["target_type"] = external_reflection_target_type(output["target_type"])
        return output

    def internal_synthesis_transition(transition: Any) -> Any:
        """External 'submit_reflection_artifacts' tool name -> internal 'submit_synthesis'; pass through all else."""
        return "submit_synthesis" if transition == "submit_reflection_artifacts" else transition

Note: with_external_target_type returns a COPY (matches existing external_reflection_state style). At the in-place assoc loops (resources.py) and the dict-mutation hydrators (reviews.py _with_snapshot/_hydrate_request/_hydrate_review) callers should instead use the scalar external_reflection_target_type on the field to preserve the exact in-place/already-copied semantics and byte-identical output. Pick the scalar form everywhere for minimal, behavior-identical edits; with_external_target_type is optional sugar — if unused, omit it to avoid dead code.

_Steps:_

1. `research_plugin/backend/domain/reflection_projection.py` (end of file after external_reflection_state (currently ends line 41); module currently imports only `from typing import Any`) — Add the two scalar helpers external_reflection_target_type and internal_synthesis_target_type, plus internal_synthesis_transition (and optionally with_external_target_type). Keep imports limited to typing (Any) so test_reflection_projection_is_domain_leaf_module's {'typing'} assertion still holds.
2. `research_plugin/backend/services/reviews.py` (import block lines 20-22, anchor `from ..domain.review_returns import resolve_review_return`) — Extend the domain import to bring in external_reflection_target_type and internal_synthesis_target_type from ..domain.reflection_projection (add a new `from ..domain.reflection_projection import (external_reflection_target_type, internal_synthesis_target_type)` line). This satisfies the layer rule (services -> domain allowed).
3. `research_plugin/backend/services/reviews.py` (lines 66-68, anchor `external_target_type = target_type` / `"synthesis" if external_target_type == "reflection"`) — Replace the inline ternary `target_type = ("synthesis" if external_target_type == "reflection" else external_target_type)` with `target_type = internal_synthesis_target_type(external_target_type)`. Keep `external_target_type = target_type` above it unchanged (still used at lines 126-130 for reviewer_handoff).
4. `research_plugin/backend/services/reviews.py` (line 198, anchor `"target_type": (` inside start() return dict) — Replace `"reflection" if req["target_type"] == "synthesis" else req["target_type"]` with `external_reflection_target_type(req["target_type"])`.
5. `research_plugin/backend/services/reviews.py` (line 367, anchor `def status(` body `target_type = "synthesis" if target_type == "reflection" else target_type`) — Replace with `target_type = internal_synthesis_target_type(target_type)`.
6. `research_plugin/backend/services/reviews.py` (lines 536-541, anchor `def _with_snapshot(self, *, row)`) — Replace the two-line `if data.get("target_type") == "synthesis": data["target_type"] = "reflection"` with `data["target_type"] = external_reflection_target_type(data.get("target_type"))` (sets key even when absent -> harmless None, but to stay byte-identical guard with `if "target_type" in data:` OR keep the conditional and just call the helper inside it). Prefer keeping the `if data.get("target_type") == "synthesis":` guard and replacing only the RHS literal `"reflection"` is not possible since helper needs the value; simplest behavior-identical form: `if "target_type" in data: data["target_type"] = external_reflection_target_type(data["target_type"])`.
7. `research_plugin/backend/services/reviews.py` (line 552, anchor `def reviewer_handoff` return dict `"target_type": "reflection" if target_type == "synthesis" else target_type`) — Replace with `"target_type": external_reflection_target_type(target_type)`.
8. `research_plugin/backend/services/reviews.py` (lines 692-697, anchor `def _hydrate_request`) — Replace `if data.get("target_type") == "synthesis": data["target_type"] = "reflection"` with `if "target_type" in data: data["target_type"] = external_reflection_target_type(data["target_type"])`.
9. `research_plugin/backend/services/reviews.py` (lines 699-706, anchor `def _hydrate_review`) — Replace the same two-line synthesis->reflection block with `if "target_type" in data: data["target_type"] = external_reflection_target_type(data["target_type"])`. Leave findings/evidence/target_snapshot lines untouched.
10. `research_plugin/backend/services/workflow.py` (import block lines 21-26, anchor `from ..ports.workflow_readers import (`) — Add `from ..domain.reflection_projection import external_reflection_target_type` to the existing domain imports (workflow.py already imports several ..domain.* modules at lines 7-20).
11. `research_plugin/backend/services/workflow.py` (line 460, anchor `def _review_gate` gate dict `"target_type": "reflection" if target_type == "synthesis" else target_type`) — Replace with `"target_type": external_reflection_target_type(target_type)`. Do NOT touch workflow.py:374-375 (gate synthesis_review->reflection_review) in this item — that is the status-half concern; leave as-is or address under a separate item to keep this edit scoped to the target_type boundary.
12. `research_plugin/backend/services/resources.py` (import block lines 21-23, anchor `from ..domain.vocabulary import GATED_ROLE_BYTE_CAPS`) — Add `from ..domain.reflection_projection import external_reflection_target_type` (resources.py already imports ..domain.markdown_images and ..domain.vocabulary, so the layer rule is satisfied).
13. `research_plugin/backend/services/resources.py` (lines 631-634, anchor `for assoc in associations:` inside _hydrate_resource) — Replace the loop body `if assoc.get("target_type") == "synthesis": assoc["target_type"] = "reflection"` with `assoc["target_type"] = external_reflection_target_type(assoc.get("target_type"))` (assoc rows always carry target_type from the SELECT, so unconditional assignment is byte-identical).
14. `research_plugin/backend/services/resources.py` (lines 929-932, anchor `for assoc in associations:` inside _hydrate_version) — Replace the identical two-line block with `assoc["target_type"] = external_reflection_target_type(assoc.get("target_type"))`.
15. `research_plugin/backend/services/reflection_tools.py` (import line 7, anchor `from ..domain.reflection_projection import external_reflection_state`) — Extend to also import internal_synthesis_transition: `from ..domain.reflection_projection import (external_reflection_state, internal_synthesis_transition)`.
16. `research_plugin/backend/services/reflection_tools.py` (lines 53-57, anchor `internal_transition = (` inside transition()) — Replace the hardcoded ternary `internal_transition = ("submit_synthesis" if transition == "submit_reflection_artifacts" else transition)` with `internal_transition = internal_synthesis_transition(transition)`. Leave the surrounding external_reflection_state wrap and self.syntheses.transition call unchanged.
17. `research_plugin/tests/workflow/test_reflection_projection.py` (after existing test methods (file ends line 50), anchor `class ReflectionProjectionTest`) — Add unit tests for the new helpers: external_reflection_target_type('synthesis')=='reflection' and passthrough for 'experiment'/None; internal_synthesis_target_type('reflection')=='synthesis' and passthrough; internal_synthesis_transition('submit_reflection_artifacts')=='submit_synthesis' and passthrough for other transitions. Import the new names at top (lines 5-8).

_Tests:_ research_plugin/tests/workflow/test_reflection_projection.py — ADD test cases for external_reflection_target_type / internal_synthesis_target_type / internal_synthesis_transition (extend the import on lines 5-8 and add 2-3 assertions). No existing assertion changes — current tests stay byte-for-byte valid.; research_plugin/tests/structure/test_service_layout.py — NO change required, but it is the guard: test_reflection_projection_is_domain_leaf_module (line 675) asserts reflection_projection imports == {'typing'} (line 677-679) and that reflection_tools/workflow_views import domain.reflection_projection. Verify it still passes (the new helpers must not add imports). NOTE the test only checks reflection_tools.py and workflow_views.py for the domain import; reviews.py/workflow.py/resources.py adding the import is allowed but unasserted.; research_plugin/tests/workflow/test_synthesis_gates.py — NO change; this is the behavior-preservation oracle (drives reflection.transition submit_reflection_artifacts + review.request target_type='reflection' end-to-end). Must stay green unchanged.

_Verify:_ cd research_plugin && ./.venv/bin/python -m pytest tests/workflow/test_reflection_projection.py tests/structure/test_service_layout.py::ServiceLayoutTest::test_reflection_projection_is_domain_leaf_module -q

_Risk:_ low · _LOC delta:_ +5

### Delete dead code and test-only compat shims (pinned.pinned_version_row, resources._get_version, sandboxes._pulled_mlflow_db_path, dedupe _tenant_for_project, delete services/reflection_policy.py shim, delete execution/errors.py + execution/types.py shims) — verified against current code.

_Current state:_ VERIFIED CURRENT LINE NUMBERS (had NOT drifted from the scan, all re-confirmed):
1) research_plugin/backend/services/pinned.py:114-141 — `def pinned_version_row(` ... ends line 141. Zero callers (grep: only the def site). Deleting it makes `from typing import Any` (pinned.py:17) unused — `Any` appears ONLY at line 17 (import) and line 121 (this fn's return annotation). `Connection` import (line 20) stays used by other fns (lines 33, 82).
2) research_plugin/backend/services/resources.py:935-947 — `def _get_version(` ... ends line 947. Zero callers (grep: only def site). `_hydrate_version` (def line 913) stays used at 406,427,637,875,911 → keep. `NotFoundError` import safe (used many places).
3) research_plugin/backend/services/sandboxes.py:1113-1117 — `def _pulled_mlflow_db_path(self, *, experiment_id: str) -> Path:` pure pass-through to `self.worker.pulled_mlflow_db_path(...)`. Only non-prod ref = tests/sandbox/test_sandbox_service.py:590. Real impl: research_plugin/backend/dataplane/worker.py:422 `def pulled_mlflow_db_path(self, *, experiment_id: str, name: str = "")`; facade exposes worker at sandboxes.py:158 (`self.worker = worker`).
4) DUP _tenant_for_project: research_plugin/backend/services/sandboxes.py:1210-1219 and research_plugin/backend/services/sandbox_provisioner.py:461-469. CORRECTION TO SCAN: NOT byte-identical — sandboxes uses `self.store.connect()`, provisioner uses `self.registry.store.connect()`; SQL `SELECT tenant_id FROM projects WHERE id = ?` and return `str(row["tenant_id"]) if row is not None else "local"` ARE identical. Method callers: sandboxes.py:180,341,972,1008,1029 and sandbox_provisioner.py:289. Both classes hold `self.registry` (SandboxRegistry). SandboxRegistry (research_plugin/backend/services/sandbox_registry.py) owns `self.store`, uses the same connect/close idiom (load_row lines 35-44), and already has TWO more inline copies of the lookup (upsert lines 176-182, record_generation lines 224-227). domain/ is INVALID home (no domain module takes a Connection; domain is pure). Correct home = SandboxRegistry.
5) research_plugin/backend/services/reflection_policy.py:1-13 — dead re-export shim. Real consumers import ..domain.reflection_policy directly (syntheses.py:23-24, experiments.py:12, tests/workflow/*). Only refs to the SHIM are test_service_layout.py:478 (asserts it is a shim). Line 224 is an independent NEGATIVE assertion.
6) research_plugin/backend/execution/errors.py:1-18 and research_plugin/backend/execution/types.py:1-26 — re-export from ..sandbox_backend. ZERO production callers (grep backend/ minus tests = empty). execution/__init__.py re-exports directly from ..sandbox_backend (not from these shims) → safe. Imported ONLY by tests/sandbox/: test_control_reaper_recovery.py:21, test_modal_sandbox_backend.py:9-10, test_cleanup.py:20, test_chaos.py:27, test_lambda_availability.py:25-26, test_sandbox_service.py:15 & :930, test_router_restart.py:19. All needed symbols present in research_plugin/backend/sandbox_backend.py __all__ (lines 270-281). Keep execution/sync_dirs.py (real, untouched).

_New shared code:_ New method on SandboxRegistry (research_plugin/backend/services/sandbox_registry.py — state-access service layer, shared collaborator already held by both callers, already owns self.store). Place in "# ---------- reads ----------" after load_row (after line 44):

    def tenant_for_project(self, *, project_id: str) -> str:
        """The owning tenant of a project (cloud plan Phase 7), 'local' default."""
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        finally:
            conn.close()
        return str(row["tenant_id"]) if row is not None else "local"

Both callers delegate to self.registry.tenant_for_project(project_id=...). No new imports, sqlite/stdlib only, no new deps, no layer-rule violation (registry already imported by both). (Optional follow-up, out of scope: collapse the two inline lookups in upsert/record_generation onto this method — leave for now to keep this change minimal.)

_Steps:_

1. `research_plugin/backend/services/sandbox_registry.py` (insert after line 44 (end of load_row, just before `def fetch_scoped` at line 46); anchor: the `finally:\n            conn.close()` block closing load_row) — ADD the shared `tenant_for_project` method (signature+body in sharedCode) into the reads section.
2. `research_plugin/backend/services/sandboxes.py` (lines 1210-1219; anchor: `def _tenant_for_project(self, *, project_id: str) -> str:` with body using `self.store.connect()`) — DELETE the private `_tenant_for_project` method (lines 1210-1219).
3. `research_plugin/backend/services/sandboxes.py` (call sites lines 180, 341, 972, 1008, 1029; anchors: `self._tenant_for_project(` (5 occurrences)) — REPLACE each `self._tenant_for_project(` with `self.registry.tenant_for_project(` (keyword args unchanged).
4. `research_plugin/backend/services/sandbox_provisioner.py` (lines 461-469; anchor: `def _tenant_for_project(self, *, project_id: str) -> str:` with body using `self.registry.store.connect()`) — DELETE the private `_tenant_for_project` method (lines 461-469).
5. `research_plugin/backend/services/sandbox_provisioner.py` (call site line 289; anchor: `tenant_id=self._tenant_for_project(project_id=project_id),`) — REPLACE `self._tenant_for_project(` with `self.registry.tenant_for_project(`.
6. `research_plugin/backend/services/pinned.py` (lines 114-141; anchor: `def pinned_version_row(`) — DELETE the entire `pinned_version_row` function (lines 114-141) and the two trailing blank lines before it (112-113) as appropriate to leave one blank line between the prior function and EOF.
7. `research_plugin/backend/services/pinned.py` (line 17; anchor: `from typing import Any`) — DELETE the now-unused `from typing import Any` import (Any was used only by the deleted function).
8. `research_plugin/backend/services/resources.py` (lines 935-947; anchor: `def _get_version(`) — DELETE the entire `_get_version` method (lines 935-947). Keep `_hydrate_version` (line 913) — still used by other methods.
9. `research_plugin/backend/services/sandboxes.py` (lines 1113-1117; anchor: `def _pulled_mlflow_db_path(self, *, experiment_id: str) -> Path:`) — DELETE the `_pulled_mlflow_db_path` pass-through method (lines 1113-1117).
10. `research_plugin/tests/sandbox/test_sandbox_service.py` (line 590; anchor: `db_path = self.app.sandboxes._pulled_mlflow_db_path(experiment_id=exp_id)`) — REPLACE with `db_path = self.app.sandboxes.worker.pulled_mlflow_db_path(experiment_id=exp_id, name=self.app.sandboxes.registry.experiment_name(experiment_id=exp_id))` (faithful repoint to the worker, matching the deleted facade's name arg).
11. `research_plugin/backend/services/reflection_policy.py` (whole file (lines 1-13); anchor: module docstring `"""Compatibility shim for project-reflection thresholds."""`) — DELETE the file research_plugin/backend/services/reflection_policy.py.
12. `research_plugin/tests/structure/test_service_layout.py` (lines 477-478; anchor: `def test_reflection_policy_service_module_is_a_compatibility_shim`) — DELETE the whole test_reflection_policy_service_module_is_a_compatibility_shim method (it asserts the deleted shim's existence). Leave line 224's negative assertion untouched.
13. `research_plugin/backend/execution/errors.py` (whole file (lines 1-18); anchor: docstring `"""Compatibility exports for sandbox backend errors."""`) — DELETE the file research_plugin/backend/execution/errors.py.
14. `research_plugin/backend/execution/types.py` (whole file (lines 1-26); anchor: docstring `"""Compatibility exports for the sandbox backend port."""`) — DELETE the file research_plugin/backend/execution/types.py.
15. `research_plugin/tests/sandbox/*.py (9 import sites)` (test_control_reaper_recovery.py:21; test_modal_sandbox_backend.py:9-10; test_cleanup.py:20; test_chaos.py:27; test_lambda_availability.py:25-26; test_sandbox_service.py:15 and :930; test_router_restart.py:19. anchor: `from backend.execution.errors import` / `from backend.execution.types import`) — REPLACE every `backend.execution.errors` and `backend.execution.types` import module path with `backend.sandbox_backend` (imported symbol lists unchanged: BackendCapabilities, SandboxRequest, BackendUnavailableError, BackendValidationError — all in sandbox_backend.__all__).

_Tests:_ research_plugin/tests/sandbox/test_sandbox_service.py:590 — repoint to worker: `self.app.sandboxes.worker.pulled_mlflow_db_path(experiment_id=exp_id, name=self.app.sandboxes.registry.experiment_name(experiment_id=exp_id))`.; research_plugin/tests/structure/test_service_layout.py:477-478 — DELETE test_reflection_policy_service_module_is_a_compatibility_shim (asserts deleted shim). Keep line-224 negative assertion.; research_plugin/tests/sandbox/test_control_reaper_recovery.py:21 — backend.execution.types -> backend.sandbox_backend (BackendCapabilities).; research_plugin/tests/sandbox/test_modal_sandbox_backend.py:9-10 — backend.execution.errors -> backend.sandbox_backend (BackendUnavailableError, BackendValidationError); backend.execution.types -> backend.sandbox_backend (SandboxRequest).; research_plugin/tests/sandbox/test_cleanup.py:20 — backend.execution.types -> backend.sandbox_backend (BackendCapabilities).; research_plugin/tests/sandbox/test_chaos.py:27 — backend.execution.types -> backend.sandbox_backend (BackendCapabilities).; research_plugin/tests/sandbox/test_lambda_availability.py:25-26 — backend.execution.errors -> backend.sandbox_backend (BackendUnavailableError, BackendValidationError); backend.execution.types -> backend.sandbox_backend (SandboxRequest).; research_plugin/tests/sandbox/test_sandbox_service.py:15 and :930 — backend.execution.types -> backend.sandbox_backend (SandboxRequest; BackendCapabilities at line 930).; research_plugin/tests/sandbox/test_router_restart.py:19 — backend.execution.types -> backend.sandbox_backend (BackendCapabilities).

_Verify:_ cd research_plugin && grep -rn --include='*.py' 'pinned_version_row' . # expect zero

_Risk:_ low · _LOC delta:_ -70

### Hoist duplicated graph payload in http_api.py and unify the dual-upstream catalog-fetch skeleton in mcp_server/proxy.py (two behavior-preserving refactors).

_Current state:_ VERIFIED against current code (line numbers drifted slightly from the scan; reported fresh below).

http_api.py (class ResearchHttpApi, defined at line 97):
- experiment_logic_graph at lines 665-718. Its tail block (lines 700-718) is: `graph: dict[str, Any] | None = None` / try json.loads(text) / isinstance(parsed, dict) -> graph=parsed / except JSONDecodeError -> graph=None / return {**base, "available": True, "resource_id": chosen.get("id"), "path": chosen.get("path"), "association_attempt_index": chosen.get("association_attempt_index"), "graph": graph, "problems": graph_problems(text), "ref_index": self.app.graph_refs.resolve_index(project_id=project_id, graph=graph)}.
- _graph_payload_for_synthesis at lines 757-813. Its tail block (lines 795-813) is BYTE-IDENTICAL to lines 700-718 (verified via exact-string equality in Python: True). Same local names in scope at both sites: base (dict), chosen (resource dict), text (str), project_id (str).
- Both blocks are reached only after their own `if text is None:` degrade-guard returns (688-699 / 783-794), so at the tail `text` is always a non-None str.
- Imports already present (lines 33-34): graph_lint (MAX_GRAPH_NODES, graph_problems) and resource_selection (preferred_associated_resource). graph_refs is reached via self.app.graph_refs (a service), so the helper CANNOT be a pure domain/ function — it must stay a private method on ResearchHttpApi.

mcp_server/proxy.py (class HttpProxyMcpServer, __init__ at lines 136-140; stdlib-only: imports json/os/sys/traceback/copy.deepcopy/dataclasses/pathlib/typing/urllib only):
- __init__ (136-140) declares only `self._project_id: str | None = None`. _scoped_cache and _plane_cache are lazily attached via getattr at 425 and 450.
- _list_tools (231-256): single-mode path (232-234) returns self._catalog_from(url=self._require_daemon_url(), is_cloud=False). Split-mode dual loop (243-256) iterates `for is_cloud,url in ((True,self.config.control_url),(False,self._daemon_url_or_none())): if not url: continue; try: for tool in self._catalog_from(...): merged[tool["name"]]=tool; except _UpstreamError: continue`.
- _catalog_from (258-271): GET {url}/mcp/tools, validate tools is list (else raise _UpstreamError), return [self._with_hidden_project_scope(tool=tool) for tool in tools if tool.get("name") != "project.list"]. Only caller is _list_tools (234, 250).
- _tool_is_project_scoped (421-445): getattr(self,"_scoped_cache",None); if None, runs the two-upstream skeleton with RAW self._http_get(url=f"{url}/mcp/tools") (pre-strip), reads inputSchema.properties for "project_id". Caches self._scoped_cache.
- _plane_for (447-470): getattr(self,"_plane_cache",None); if None, runs the same skeleton with raw _http_get, reads tool["plane"] (a field _with_hidden_project_scope STRIPS at 512). setdefault(name,plane). Caches self._plane_cache; default "control".
- _with_hidden_project_scope (507-522) strips both "plane" and project_id, so _tool_is_project_scoped and _plane_for MUST read the RAW catalog, not _catalog_from output.
- No test references the internal proxy method names; proxy refactor is purely internal.

CRITICAL TEST COUPLING: research_plugin/tests/structure/test_service_layout.py::test_transport_delegates_graph_ref_resolution_to_service (lines 1185-1197) asserts `source.count('"ref_index": self.app.graph_refs.resolve_index(') == 2` at lines 1188-1190. Hoisting the two identical tails into one helper drops that to 1, so the assertion MUST change 2 -> 1. Sibling assertions (assertIn at 1187; assertNotIn at 1191-1193) stay satisfied.

_New shared code:_ TWO new private helpers, each in its existing file; neither is a domain/ leaf because both dereference instance/service state (domain rule respected by NOT moving them there).

1) http_api.py, class ResearchHttpApi (insert after _graph_payload_for_synthesis, ~line 813, before _association_pinned_text at 815). Stays in transport layer (touches self.app.graph_refs):
    def _graph_payload(self, *, base: dict[str, Any], chosen: dict[str, Any], text: str, project_id: str) -> dict[str, Any]:
        """Parse + lint + resolve-refs the available-graph tail shared by the
        experiment and synthesis graph endpoints (byte-identical payload)."""
        graph: dict[str, Any] | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                graph = parsed
        except json.JSONDecodeError:
            graph = None
        return {
            **base, "available": True,
            "resource_id": chosen.get("id"), "path": chosen.get("path"),
            "association_attempt_index": chosen.get("association_attempt_index"),
            "graph": graph, "problems": graph_problems(text),
            "ref_index": self.app.graph_refs.resolve_index(project_id=project_id, graph=graph),
        }
   Both sites end with: return self._graph_payload(base=base, chosen=chosen, text=text, project_id=project_id)

2) proxy.py, class HttpProxyMcpServer (insert after _catalog_from, ~line 271). stdlib-only (no new third-party imports; uses self._http_get/self.config/self._daemon_url_or_none/_UpstreamError):
    def _each_catalog_tool(self) -> Iterator[tuple[bool, dict[str, Any]]]:
        """Yield (is_cloud, raw_tool) for every reachable upstream's /mcp/tools.
        Raw = pre-strip, so callers can read 'plane' and project_id schema. A
        down upstream is skipped, never fatal."""
        for is_cloud, url in ((True, self.config.control_url), (False, self._daemon_url_or_none())):
            if not url:
                continue
            try:
                body = self._http_get(url=f"{url}/mcp/tools", is_cloud=is_cloud)
            except _UpstreamError:
                continue
            for tool in body.get("tools") or []:
                if isinstance(tool, dict):
                    yield is_cloud, tool
   Add Iterator to the existing `from typing import Any, TextIO` -> `from typing import Any, Iterator, TextIO` (stdlib typing; ProxyStdlibOnly test still passes).

_Steps:_

1. `research_plugin/backend/http_api.py` (Insert after _graph_payload_for_synthesis (currently ends at line 813), immediately before `def _association_pinned_text(self, resource: dict[str, Any]) -> str | None:` (line 815). Anchor: "    def _association_pinned_text(self, resource") — ADD the new `def _graph_payload(self, *, base, chosen, text, project_id)` method whose body is the byte-identical tail block from sharedCode (the json.loads -> isinstance(dict) -> graph_problems -> graph_refs.resolve_index assembly).
2. `research_plugin/backend/http_api.py` (experiment_logic_graph tail, lines 700-718. Anchor (first line to replace): "        graph: dict[str, Any] | None = None" that immediately follows the experiment `if text is None:` block's closing (the chunk ending at the experiment return whose base also carries experiment_id).) — REPLACE lines 700-718 (the whole tail block) with: `        return self._graph_payload(base=base, chosen=chosen, text=text, project_id=project_id)`. (base already carries experiment_id/max_nodes/experiment_status/attempt_index from lines 681-686, available/resource_id/etc come from the helper.)
3. `research_plugin/backend/http_api.py` (_graph_payload_for_synthesis tail, lines 795-813. Anchor: the SECOND `        graph: dict[str, Any] | None = None` in the file (inside _graph_payload_for_synthesis, after its `if text is None:` return at 783-794).) — REPLACE lines 795-813 with: `        return self._graph_payload(base=base, chosen=chosen, text=text, project_id=project_id)`. (base already set at 770 + synthesis sub-dict at 776-782.)
4. `research_plugin/mcp_server/proxy.py` (Import line, currently `from typing import Any, TextIO` (line 41). Anchor: "from typing import Any, TextIO") — REPLACE with `from typing import Any, Iterator, TextIO` (needed for the generator's return annotation; stdlib so stdlib-only test is unaffected).
5. `research_plugin/mcp_server/proxy.py` (__init__, lines 136-140. Anchor: "        self._project_id: str | None = None") — ADD two lines directly below it: `        self._scoped_cache: set[str] | None = None` and `        self._plane_cache: dict[str, str] | None = None` (replaces the getattr-grown caches; declared beside _project_id as the item requires).
6. `research_plugin/mcp_server/proxy.py` (Insert after _catalog_from (ends line 271), before `# ---- tools/call` comment at line 273. Anchor: "    # ---- tools/call ---") — ADD the new `def _each_catalog_tool(self) -> Iterator[tuple[bool, dict[str, Any]]]:` generator from sharedCode (encapsulates the for-(is_cloud,url)-in-two-upstreams / skip-empty / GET /mcp/tools / except _UpstreamError: continue / yield raw tool skeleton).
7. `research_plugin/mcp_server/proxy.py` (_list_tools split-mode dual loop, lines 243-256. Anchor: "        for is_cloud, url in (\n            (True, self.config.control_url)," (the loop populating `merged`).) — REPLACE lines 243-256 with a single loop over the generator that re-applies the project.list filter + strip (logic currently inside _catalog_from): `        for is_cloud, tool in self._each_catalog_tool():\n            if tool.get("name") == "project.list":\n                continue\n            shaped = self._with_hidden_project_scope(tool=tool)\n            merged[shaped["name"]] = shaped\n        return list(merged.values())`. Keep the surrounding comments (240-242). NOTE: _catalog_from is still used by the single-mode path at line 234, so it stays (its tools-is-list validation is preserved there). Behavior preserved: cloud-first/daemon-second iteration order is identical, so daemon schemas still win on overlap.
8. `research_plugin/mcp_server/proxy.py` (_tool_is_project_scoped, lines 421-445. Anchor: "        scoped = getattr(self, \"_scoped_cache\", None)") — REPLACE the body so it uses the declared cache + the generator: `        if self._scoped_cache is None:\n            scoped: set[str] = set()\n            for _is_cloud, tool in self._each_catalog_tool():\n                schema = tool.get("inputSchema")\n                props = schema.get("properties") if isinstance(schema, dict) else None\n                if isinstance(tool.get("name"), str) and isinstance(props, dict) and "project_id" in props:\n                    scoped.add(tool["name"])\n            self._scoped_cache = scoped\n        return name in self._scoped_cache`. Keep the leading comment (422-424). Pre-strip schema read preserved (generator yields raw tools).
9. `research_plugin/mcp_server/proxy.py` (_plane_for, lines 447-470. Anchor: "        planes = getattr(self, \"_plane_cache\", None)") — REPLACE the body to use the declared cache + the generator: `        if self._plane_cache is None:\n            planes: dict[str, str] = {}\n            for _is_cloud, tool in self._each_catalog_tool():\n                plane = tool.get("plane")\n                if isinstance(tool.get("name"), str) and isinstance(plane, str):\n                    planes.setdefault(tool["name"], plane)\n            self._plane_cache = planes\n        return self._plane_cache.get(name, "control")`. Keep the leading comment (448-449) and the trailing default-control comment (468-470). Pre-strip 'plane' read preserved (generator yields raw tools before _with_hidden_project_scope pops 'plane').
10. `research_plugin/tests/structure/test_service_layout.py` (test_transport_delegates_graph_ref_resolution_to_service, lines 1188-1190. Anchor: "            source.count('\"ref_index\": self.app.graph_refs.resolve_index('), 2") — CHANGE the expected count from 2 to 1 (the two byte-identical tails are now a single helper). Leave assertIn at 1187 and the assertNotIn lines 1191-1193 unchanged.

_Tests:_ research_plugin/tests/structure/test_service_layout.py: in test_transport_delegates_graph_ref_resolution_to_service (lines 1185-1197), change the count assertion at lines 1188-1190 from expected 2 to expected 1 (hoist collapses the two byte-identical occurrences into one helper). Lines 1187 and 1191-1193 stay as-is.; No proxy test changes needed: grep of research_plugin/tests/ finds no reference to _list_tools/_catalog_from/_tool_is_project_scoped/_plane_for/_scoped_cache/_plane_cache/_each_catalog_tool. Merged-catalog, project-scoping and plane-routing behavior is asserted through public tools/list + tools/call in test_proxy_split.py / test_proxy_mcp.py and is unchanged.

_Verify:_ grep -c '"ref_index": self.app.graph_refs.resolve_index(' research_plugin/backend/http_api.py  # expect 1 after hoist (was 2)

_Risk:_ low · _LOC delta:_ -30

## Shared-file conflicts & ordering

- research_plugin/backend/services/experiments.py — touched by item1 (covered_terminal_ids, edits _terminal_experiments_since_last_reflection at 242-251 + import line 12) and item2 (review_snapshot_id, edits _target_snapshot_id body 717-731 + adds import). BOTH IN PHASE 1, sequential not parallel. Required order: item1 then item2 (item1 extends the existing reflection_policy import; item2 adds a fresh import line — doing item1 first avoids re-resolving the import block).
- research_plugin/backend/services/syntheses.py — touched by item1 (covered_ids in reflection_signal 1310-1324 + reflection_policy import 22-25) and item2 (_target_snapshot_id body 1204-1218 + adds import). BOTH IN PHASE 1, sequential. Required order: item1 then item2 (same reason as experiments.py).
- research_plugin/backend/services/resources.py — touched by item5 (target_type assoc-loop renames at 633 and 931) and item7 (delete _get_version at 935-947). Required order: item5 (Phase 4) BEFORE item7 (Phase 5). Rationale: item5 edits lines just above _get_version; deleting _get_version first would shift nothing for item5 but doing the rename first keeps both edits at stable, independently-verified line numbers. The two regions do not overlap (633/931 rename vs 935-947 delete).
- research_plugin/tests/structure/test_service_layout.py — touched by item6 (change graph_ref resolve_index count from 2 to 1 at line 1189) and item7 (delete test_reflection_policy_service_module_is_a_compatibility_shim at line 477). Required order: item6 (Phase 3) BEFORE item7 (Phase 5). The two edits are in disjoint regions (line 1189 vs line 477) so they cannot collide, but sequencing them across phases keeps each phase's gate self-consistent.
- research_plugin/tests/sandbox/test_sandbox_service.py — touched ONLY by item7 (two edits in one item: repoint _pulled_mlflow_db_path call at line 590 to worker.pulled_mlflow_db_path, and repoint execution.types imports at lines 15/930 to backend.sandbox_backend). No cross-item conflict; both edits land together in Phase 5.

## Suggested commit sequence

1. refactor(domain): unify reflection-drift covered-terminal-id computation into domain/reflection_policy.covered_terminal_ids; call from ExperimentService and SynthesisService
2. refactor(domain): extract byte-stable review snapshot-id into domain/review_snapshot.review_snapshot_id; delegate from both service _target_snapshot_id wrappers + add format-locking unit test
3. refactor(ui): extract shared synthesis wave role-resolution model into components/synthesis/waveModel.js; consume from desktop ProjectSynthesisPanel and mobile MobileSynthesisScreen
4. refactor(execution): collapse ssh_rsync _pull/_push_command into one _rsync_command(push=...) and extract the shared _run_passes run-loop/result assembly
5. refactor(transport): hoist the duplicated graph payload tail into ResearchHttpApi._graph_payload and unify the dual-upstream catalog fetch into proxy._each_catalog_tool; update graph_ref count assertion 2->1
6. refactor(domain): route internal-synthesis/external-reflection target_type and inbound tool-name renames through domain/reflection_projection helpers; extend reflection_projection unit tests
7. chore(cleanup): delete dead code (pinned_version_row, resources._get_version, sandboxes._pulled_mlflow_db_path) and compat shims (services/reflection_policy.py, execution/errors.py, execution/types.py); dedupe _tenant_for_project onto SandboxRegistry.tenant_for_project and repoint sandbox tests