# Frontend Redesign Objectives

Status: living design brief · Last updated: 2026-06-01

This document captures the conclusions of a UX deep-dive on the current
`research_state_ui` frontend, plus the product decisions made in response. It is
the reference for the next rebuild of the UI. It records (a) what the product is
for, (b) what is wrong with the current UI, (c) the decisions that are now
locked, and (d) the questions still deferred.

The audit was done against the live app (the data-rich "EEG Research" project:
4 claims, 6 experiments, 92 resources, 20 open reviews), not just the source.

---

## 1. Product goal and user

**User:** a Machine Learning Scientist.

**Platform goal:** automate the *idea → feedback* cycle so that researchers can
generate **meta-ideas** quickly. The human should be lifted up one level — out
of running individual experiments and into reasoning about which directions to
pursue — while the agent drives the loop below them.

**Why the experiment lifecycle is a predictable loop:** achieving that goal
requires a *simple and predictable workflow process* for experiments. The
experiment FSM is intentionally a fixed loop so the human can build a reliable
mental model of "where is this in the cycle" without relearning it each time.

**The research process the platform automates:**

```
        ┌──────────────────────────────────────────────┐
        │                                               │
        ▼                                               │
  Generate ideas  ⇄  Learn from prior research          │
                            │                           │
                            ▼                            │
                     Run experiments ──▶ Refine ─────────┘
                                          │
                                          ▼
                                     New Outcomes
```

Generate ideas and learning from prior research feed each other; that produces
experiments to run; running leads to refinement; refinement both yields new
outcomes and loops back to generate the next ideas. The UI exists to make the
*state* of this loop legible to the scientist at a glance.

---

## 2. What the product actually is (the design lens)

This is **not** a tool the human operates step-by-step. It is a **window onto an
autonomous agent (Codex, over MCP) running the research loop**. The human's job
is **supervision and orientation**: understand where things stand, notice what
needs attention, and decide the next meta-move.

Almost every problem below traces to one root cause: **the current UI is built
as a faithful database viewer, when it needs to be a supervisor's
situational-awareness layer.**

---

## 3. UX diagnosis of the current UI

### Core problems

1. **It renders the agent's raw output at full length instead of summarizing or
   staging it.** The running experiment's detail page is **~14,400px tall — 16
   full screens**. It inlines the entire 15.2 KB design plan as prose, then the
   entire verbatim review, paragraph after paragraph. The agent produces walls
   of text and the UI does zero compression and no progressive disclosure.

2. **There is no "what needs me" surface.** Home shows counts and a
   recent-events log, but never "here is what is blocked / waiting / in flight."
   For a supervisor, the first question — *where do I look right now?* — has no
   home, so the user scans everything and gets lost.

3. **The visual system is monochrome and flat, so nothing has weight.** Tokens:
   background `#fafaf7`, text `#1a1a1a`, grays for everything else, base font
   **13.5px**, content locked to a **720px column** in a 1280px+ window. The only
   color in the system is the status pill. Every page looks the same → reads as
   "wall of text."

4. **The IA is organized by entity type, not by the research narrative.** Ten
   nav destinations, most of which are a flat table of one entity. But the
   product *is* the relationships: claim → approach → attempt → outcome. That
   story lives in exactly one place (the Logic DAG) and is crammed into the
   720px text column as an unreadable thumbnail with a long prose legend.

### Secondary problems

- **Events / Live-traffic are debug logs surfaced as primary nav.** Home's event
  feed is a dozen identical `resource.observed res_…d8e02f` lines; "Live traffic"
  is rows of `MCP · ok · 2158ms` with no method name. Low signal.
- **Identity is illegible:** everything is a truncated hash (`exp_…a39d28`,
  `res_…d8e02f`, `rr_…9cade8`) the human cannot hold in their head.
- **Horizontal space is wasted** — single narrow column → everything stacks →
  multi-screen scrolls; no master-detail or side-by-side.
- **Cryptic glyphs without in-context legend** (e.g. the `•••` markers on
  claims).

---

## 4. Decisions (locked for the next build)

These are the answers given in response to the audit. Treat them as constraints.

1. **User & goal — settled.** ML Scientist; automate idea→feedback so the human
   operates at the meta-idea level. Keep the experiment loop simple and
   predictable (Section 1).

2. **Landing page = "State of Research" with active pulses.** A **canvas-style
   layout of the whole research state**, with visual **pulses around the things
   that are currently active or require attention**. The landing screen's job is
   orientation across the entire loop, not a list of one entity type.
   - Implication: this is naturally the claim → approach → attempt → outcome
     graph, made first-class, live, and given real space — the opposite of the
     current buried thumbnail DAG.

3. **Altitude = status-first, then drill into ground truth — but stay close to
   markdown.**
   - The user does **not** need the full plan/review immediately. Start with a
     **zoomed-out, status-level signal**, then let them drill into specifics.
   - **Do not build a UI-side summarizer.** Stay close to **rendering the
     agent's markdown files directly** — that is the native channel through which
     the agent controls what the user sees, and it keeps the UI anchored to
     ground truth.
   - **Lever: instruct the agent (via skill/SKILL.md) to author its markdown in a
     prescribed, readable structure** (e.g. lead with a short status/TL;DR block,
     consistent headings) so readability improves at the source. The UI renders
     faithfully; the agent does the shaping.

4. **Read-only for now.** This is a **navigation and observation layer**.
   Intervention/controls are deferred to a later version. **Current input method:
   the user goes back to the agent through the plugin and tells it what to do.**
   - Implication: do not invest in mutation flows / action buttons now. The build
     optimizes for *reading and orienting*, not *operating*.

5. **Keep a simple, predictable left nav with native types.** Retain
   **Claims / Experiments / Resources** in the left pane, **keeping the file tree
   inside Resources** — even if this isn't the highest-value navigation, its
   predictability is worth keeping.

6. **Primary unit of attention: tentatively the Experiment.** Not finalized, but
   "experiment" is a solid default for the thing the user checks on.

7. **The Activity section can probably go away** (Events / Live MCP traffic) —
   debug logs, not human-facing signal.

8. **The file tree sharing a column with global navigation is acceptable** — not
   a problem worth solving now.

---

## 5. Design objectives that follow from the decisions

- **Make a "State of Research" canvas the home**, with the claim→approach→
  attempt→outcome structure as its spine and pulses on active/attention nodes.
- **Two-altitude pattern everywhere:** status-level signal first, drill-through to
  the agent's raw markdown second. No 16-screen default pages.
- **Render markdown faithfully; shape readability at the source** by updating the
  agent's authoring instructions, not by adding a UI summarizer.
- **Keep the predictable Claims / Experiments / Resources nav** with the file tree
  under Resources.
- **Optimize for reading/observation**, not mutation (read-only era).
- **Remove the Activity/Live-traffic surfaces** from primary nav.
- **Introduce visual hierarchy** so attention is drawn, not earned by scanning:
  selective weight/color and use of horizontal space (these specifics are still
  open — see Section 6).

---

## 6. Deferred / open questions

Explicitly deferred for now:

- What deserves color, weight, and space — and what should recede? (The
  monochrome-minimal identity may stay, but then hierarchy must come from type
  scale, weight, and spacing.)
- Exactly how the markdown "status block" convention should look, and what the
  agent authoring instructions become.
- How identity should read to a human (names, slugs, breadcrumbs vs. raw hashes).
- Single narrow column vs. master-detail / multi-pane layouts.
- Live/real-time treatment (how "pulses" actually animate; polling vs. push).
- Whether the primary unit of attention stays the Experiment.
- Visual identity / type system specifics.
- Mobile/responsive: assumed out of scope, to be confirmed.

---

## 7. Source material

- Audit conducted against the running app (preview server) on the "EEG Research"
  project, 2026-06-01.
- Current frontend: `research_state_ui/` (React + Vite + Zustand; ~11.5k LOC,
  4.3k-line `global.css`).
- Related docs: `UI_API.md`, `CLAUDE_FRONTEND_HANDOFF.md`, `RESOURCE_MODEL.md`,
  `WORKFLOW_AND_REVIEW.md`, `ARCHITECTURE.md` (this folder).
