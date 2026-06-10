# Research Plugin — Agent Tool Reflection (Round 1)

_Date: 2026-06-09. Method: empirical mining of a real agent session + live-agent
interviews, with every headline claim verified against the code._

## How this was produced (repeatable)

Two complementary lenses, then a skeptical pass:

1. **Empirical** — a read-only snapshot of `tool_calls.sqlite` from the live
   project `research_system_test_4` (1,500 real MCP calls, session of
   2026-06-07). Every call has `args_json`, `received_chars`, `status`,
   `error_code`, `result_truncated`, `duration_ms`. Four analyzers mined it
   (resources / sandbox / review+experiment / macro).
2. **Experiential** — three fresh agents drove the tools through the real
   dispatch path against an **isolated** daemon (`scripts/_reflection_daemon.py`:
   ephemeral port, scratch project, in-memory fake backend in Lambda-style
   selection mode → zero cloud cost, never touches the live `:8787` daemon).
   Personas: procure-compute, run-&-monitor, manage-knowledge.
3. **Adversarial verify** — each candidate finding was checked against the
   actual contracts/services; only code-confirmed gaps are kept. This pass
   debunked four plausible-but-wrong findings (see "Corrections").

42 raw findings → cross-corroborated and code-verified below.

## Executive summary

- **96.8% of all 1,500 tool calls were two busy-poll loops**: `resource.list`
  (744) and `sandbox.terminal` (708). Everything else combined was 48 calls.
- **`resource.list` produced 200.1 MB — 92.7% of all 215.85 MB of tool output
  this session** — by re-fetching the *same unfilterable, fully-hydrated list*
  744 times. There is no filter, no pagination, no projection, no "changed
  since". `ResourceListInput` is literally `pass` (only inherits `project_id`).
- **`sandbox.terminal` was polled up to 304 times in an unbroken run.** It has
  no incremental cursor (every poll re-pulls the full `tail`) and returns no
  structured `running` / `exit_code` / `finished_at` — so the agent cannot tell
  when a command finished and busy-polls instead.
- **`review.submit` rejects unknown fields (`extra="forbid"`) but its
  `findings`/`evidence` are untyped `dict`s with no sub-schema or description** —
  the agent guessed the shape, invented a field, and got a bare
  `validation_error`.
- The path to a sandbox is a **multi-step precondition chain** whose
  requirements surface **one error at a time** (e.g. the experiment-plan
  *section-header* requirement only appears as the *third* error).

## Lens 1 — Tools / affordances the agent needed but didn't have

| # | Tool | Gap | Evidence |
|---|------|-----|----------|
| 1 | `sandbox.terminal` | **No incremental "new output since cursor".** Each poll re-pulls the whole tail. | Contract exposes only `tail` (`contracts.py:255-257`). 708 polls, longest unbroken run **304**. |
| 2 | `sandbox.terminal` / new `sandbox.exec` | **No structured "is the command still running / exit code / finished_at".** | Service returns only `{status, transcript}` (`sandboxes.py:506-511`). Agent kept polling a *terminated* sandbox. |
| 3 | `resource.list` | **No "changed since" / list-level version or etag** for cheap change-detection. | Per-resource `version_token` exists but is not queryable at list level (`resources.py`). |

(A blocking "wait until exits" was also requested, but #1 + #2 — a cursor plus an
`exit_code`/`running` field — largely obviate it and compose better with the
existing async model.)

## Lens 2 — Outputs that emitted info the agent didn't need (noise)

| # | Tool | Noise | Evidence |
|---|------|-------|----------|
| 4 | `resource.list` | **Fully-hydrated payload with no compact mode.** Each item embeds the entire nested `current_version`, duplicating ~top-level fields. | `_hydrate_resource` sets `data["current_version"] = _hydrate_version(...)` (`resources.py:359-363`), called per row (`:224`). 200.1 MB / 744 calls ≈ **269 KB each**. |
| 5 | `sandbox.terminal` | **Re-emits the full 50 KB tail every poll** even when nothing changed (see #1). | 15.5 MB over 708 polls. |

## Lens 3 — Outputs that didn't emit info the agent needed

| # | Tool | Missing field | Evidence |
|---|------|---------------|----------|
| 6 | `resource.list` | **No filter/pagination/projection params at all.** | `class ResourceListInput(ProjectScopedInput): pass` (`contracts.py:141-142`). |
| 7 | `review.submit` | **`findings`/`evidence` are untyped `dict`/`list[dict]` with no sub-schema or field docs**, while `ContractModel` is `extra="forbid"`. Agent can't know the accepted shape. | `contracts.py:12-15` (`extra="forbid"`), `:172-173` (untyped). Real `validation_error` in the log. |
| 8 | `experiment.get_state` / transition errors | **Doesn't surface allowed-next transitions or unmet preconditions.** Agent learns requirements only by trial-and-error. | `submit_design` → "plan resource must be synced"; then a *second* error for association; then a *third* for required plan **section headers**. |

## Workflow friction

- **`sandbox.request` precondition chain** (`planned → submit_design → design_review
  → [passing review] → mark_ready_to_run → ready_to_run → sandbox.request`) is
  discovered only via errors. `sandbox.request`'s error names the required
  *status* but not *how to reach it*.
- **`review.request` re-review** failed `permission_denied`: the experiment must
  first be transitioned back into the reviewable state; the error didn't say so.

## Unused / undiscovered tools

11 of 34 tools were never called, including `sandbox.options`, `sandbox.sync`,
`resource.sync_changed_files`, `resource.history`, and most `claim.*`. Most are
expected for this session's shape. **Note:** the verify pass found
`sandbox.options` is *not* hidden — the `needs_selection` response already
advertises it — so its non-use is a workflow-shape artifact, not a
discoverability bug.

## Corrections (claims the verify pass debunked — kept for honesty)

- **"`resource.list` is always truncated / the agent received clipped JSON."**
  **FALSE.** The 256 KB cap (`DEFAULT_MAX_PAYLOAD_CHARS`, `tool_calls.py:38`)
  applies only to the **debug telemetry log**; `result_truncated=1` means the
  *logged copy* was clipped, not the agent's payload. The agent received the
  full ~269 KB. (The real problem is the *size and redundancy*, not corruption.)
- **"`sandbox.options` is undiscoverable from `sandbox.request`."** FALSE — the
  selection response links to it.
- **"`sandbox.request` blocks ~21 s with no async option."** FALSE —
  asynchronous provisioning is already the default (`sandboxes.py:329-349`
  returns a `provisioning` status); the 21 s reflects the agent choosing to
  wait, not a missing affordance.

## Prioritized recommendations

| Pri | Recommendation | Tool(s) | Severity | Effort |
|-----|----------------|---------|----------|--------|
| 1 | Add a **cursor/`since`** (return only new bytes + a cursor token) **and** structured `running`/`exit_code`/`finished_at` fields | `sandbox.terminal` | High | S–M |
| 2 | Add **filters** (`kind`/`status`/`experiment_id`), **pagination/`limit`**, and a **compact/projection mode** (drop nested `current_version` by default) | `resource.list` | High | M |
| 3 | Add a **list-level version/etag or `changed_since`** for cheap polling | `resource.list` | Med | S–M |
| 4 | **Document/type `findings` & `evidence`** in the tool description and return validation errors that list accepted fields (or relax `extra="forbid"` for these) | `review.submit` | Med–High | S |
| 5 | **Surface allowed-next transitions + unmet preconditions up front** (in `experiment.get_state` and in errors) instead of one-at-a-time | `experiment.*` | Med | M |

The top two recommendations would, on this session's evidence, have eliminated
the loops that produced **96.8% of calls and ~215 MB of output**.

## Status — implemented in this round (2026-06-09)

- ✅ **`resource.list`**: added `kind`/`experiment_id`/`missing` filters,
  `limit`/`offset` pagination (response carries `total`/`count`/`has_more`), and
  `compact=true` (lean projection that omits the heavy nested `current_version`;
  keeps `version_token` for cheap change-detection). _Rec. 2 + 3._
- ✅ **`sandbox.terminal`**: added a `since` cursor (every response returns a
  `cursor`; poll with `since=cursor` to get only new output) and a `running`
  flag (stop polling a finished sandbox). _Rec. 1, partial._
- ✅ **`review.submit`**: `findings`/`evidence` now carry field-level schema docs
  + the tool description lists accepted fields and says structured rationale goes
  in `evidence` (unknown top-level fields are rejected). _Rec. 4._
- ✅ **Experiment transitions**: `experiment.get_state` now returns
  `allowed_transitions` (each with `leads_to` + a `requires` precondition hint),
  and the "not allowed" error lists what *is* allowed. _Rec. 5._
- ✅ **`sandbox.terminal` per-command status**: now parses the `rec.sh`
  transcript markers (`[<ts>] $ <cmd>` / `[<ts>] (exit <rc>)`, already emitted
  via `PIPESTATUS[0]`) and returns `last_exit_code`, `last_command_finished_at`,
  and `command_running` — so an agent can tell when a command finished and
  whether it succeeded instead of busy-polling. Best-effort/null on sandboxes
  created before the markers existed. _Rec. 1, remainder._

All landed with tests; full suite green (170 passed).

## Methodology note

Empirical N = 1,500 real calls; interviews via a fake-backed isolated daemon;
every headline claim cited to a file:line. This is **Round 1** of a repeatable
practice — re-run against future sessions to track whether tool ergonomics
improve. Harness lives in-tree: `scripts/_reflection_daemon.py` +
`FakeSandboxBackend(requires_hardware_selection=True)` + the snapshot/query
recipe above.
