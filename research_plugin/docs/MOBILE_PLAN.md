# Mobile Version — Build Plan

**Status:** proposed, 2026-06-12.
**Scope:** make the research dashboard usable from a phone for an away-from-desk supervisor.
**Process:** front-end mapped exhaustively (9 parallel readers over every source slice + docs +
backend serving path); three independent architecture designs (responsive retrofit / separate
companion PWA / product-first) scored by three judges (user lens, maintainer lens, shipping lens);
the winner red-teamed adversarially. This document is the synthesis: the winning design with the
judges' grafts merged and the red-team's conditions folded in as plan, not caveats.

---

## 1. What the front-end is today (verified map)

`research_state_ui/` — React 18.3 (plain JSX), Vite 5, react-router-dom 6 (BrowserRouter, 15
routes), zustand 5 (one store), @xyflow/react 12 (two ReactFlow canvases), react-markdown 10 +
remark-gfm, prism-react-renderer. ~11.5k LOC, no TS, no tests, no lint. One 4,088-line
`global.css` with a ~60-token design system and full light/dark theming (pre-paint
`rsui:theme` script in index.html). Production build: one unsplit 782 KB JS chunk.

**Data layer (mobile-ready):** HTTP polling only — no SSE/WebSocket anywhere. One
visibility-aware `usePolling(3000)` fires 3 parallel GETs per tick (`/home` fat bootstrap,
`/sandboxes`, `/events?limit=500`). ExperimentDetail adds per-segment pollers: status 3s,
figure 3s, logic graph 3s (both canvases stay mounted and both poll), sandbox+metrics 3s,
terminal 1.5s with a `tail`/`since` byte cursor. The workflow is server-authoritative: GateBanner
renders `workflow.next_action`/`allowed_actions` verbatim; the UI never computes the FSM.

**Presentation layer (desktop-only):** exactly 3 media queries in 4,088 lines. Fixed 260px
sticky sidebar holding *all* navigation, never collapses. `.page-stage` 1120px + fixed 40px
padding. Grid "infra tables" with hard min-widths (Sandboxes 840px, Experiments 720px, Debug
720/640px). ReactFlow canvases at fixed 400px with JS-encoded layout constants (196px nodes),
touch scroll-trap, node-detail panel that stacks *below* the canvas under 900px. VisualDag: a
755-line, 1600×820 hover-only SVG — dead on touch. MLflow/TensorBoard iframes at fixed 540px
(Lambda's dashboard URLs are `127.0.0.1` SSH-forwards — unreachable from any other device).
PDF via bare iframe (iOS renders page 1 only). Hover `title=` tooltips carry real information
(full ids, timestamps, durations, intents, sync errors). Touch targets ~26px. No
dvh/svh, no PWA manifest, no service worker. `navigator.clipboard` silently fails over plain
HTTP.

**Serving/security (hard blockers):** daemon binds `127.0.0.1:8787`; **no auth on any
endpoint**; CORS `allow_origins=['*']`; the daemon does **not** serve the built UI (no
StaticFiles mount, no SPA fallback). Unauthenticated destructive endpoints exist (`POST
…/sandbox/release` kills a GPU VM; `POST …/transition`; `DELETE …/resources/{id}`), and the
whole mutation surface is *also* reachable via `POST /mcp/call` (http_api.py:996).

**Good bones to reuse unmodified:** api.js (181 lines) + identity-stable selectors; the token
/theme system; FSMStrip (pure CSS); GateBanner contract; `planSections.js` (progressive plan
disclosure); `terminalBlocks.js` (per-command transcript folds); `evidence.js` (✓✗?◐· outcome
taxonomy); deterministic graph layouts (same JSON → same positions across polls); the archived
MLflow metrics endpoint (`…/results/metrics`, ≤1000 downsampled points) — which can replace
the iframes entirely and outlives the VM.

## 2. Decision: responsive retrofit — "one app, two shells"

The judges' consensus (and mine): the data layer is mobile-ready today; only the presentation
shell and a handful of desktop-physics components are not. So:

- **One codebase, one router, one store.** No second app: a separate companion PWA pays a
  shared-layer extraction across an 11.5k-LOC zero-test codebase *before* any mobile value
  ships, plus a permanent drift tax — disqualifying for a solo maintainer.
- **Viewport-gated shell, not media-query archaeology.** A `useViewport()` gate renders
  `<MobileShell>` (app bar + 4-tab bottom nav: Now / Experiments / Activity / More) instead of
  the sidebar shell. Gate on capability + smaller dimension — `pointer: coarse &&
  min(screen.w, screen.h) <= 768` — so a rotated phone stays mobile and an iPad stays desktop;
  persist a `rsui:surface` manual override in localStorage as the escape hatch.
- **Mobile gets its own landing.** Route `/` renders a new `NowScreen` on mobile, never
  Home.jsx. This de-conflicts with the locked desktop "State of Research" canvas redesign:
  mobile work never edits the desktop pages that redesign will replace.
- **Replace, don't reflow.** Min-width tables become card lists (`ExperimentCardList`,
  `SandboxCardList`) on mobile; the desktop grid CSS is untouched. All mobile styles live in a
  media-scoped `src/styles/mobile.css` (~700 lines); `global.css` gets exactly two mechanical
  edits (`.page-stage` padding → var, dvh fallbacks) landed as one tiny early PR. **No touch-
  target bumps in global.css** — that keeps the desktop redesign's slate clean and "desktop
  pixel-identical" plausible without a test suite.
- **Read-only era holds, one exception.** No transition/approve/delete affordances on mobile.
  The single sanctioned mutation is **sandbox release** (an expiring GPU VM is exactly the
  away-from-desk emergency), behind slide-to-confirm, with a red second-acknowledgment when
  `active_experiments` shows a running experiment attached to that sandbox. Record in
  FRONTEND_REDESIGN_OBJECTIVES.md: *release is the only mobile mutation, ever.*

New code: ~2k LOC of presentation in `src/mobile/` + `mobile.css`. Zero data-layer fork.

## 3. The red-team's binding conditions (folded into Phase 0/1 below)

The adversarial pass surfaced one fatal and several serious flaws in the winning design as
originally written. These are now plan requirements, not notes:

1. **Default-deny auth, covering `/mcp/*`.** Token middleware scoped to `/api/*` would have
   left the entire mutation surface unauthenticated via `POST /mcp/call` while Tailscale
   widened its reach — and the SPA fallback as written would have swallowed `GET /mcp/tools`
   with HTML, breaking the agent's own MCP proxy (mcp_server/proxy.py discovers the daemon via
   `daemon.json` and calls `/mcp/tools`). Middleware requires the token for everything except
   `/health` and static assets; the SPA fallback exempts `/api/*` *and* `/mcp/*`; proxy.py
   reads the token from the `daemon.json` marker it already parses; same-commit consumer
   matrix: `scripts/dev_http_reload.py`, `scripts/_reflection_daemon.py`,
   `tests/surface/test_proxy_mcp.py`, all `.mcp.json` client configs. Surface test: 401 on
   `/mcp/call` without a token.
2. **Cookies are GET-only.** The `rsui_token` cookie (needed for iframe/img src URLs) is
   `SameSite=Strict` and accepted only for safe methods; every mutating verb requires the
   `Authorization` header (api.js already centralizes this). Host-header allowlist (ts.net
   name, localhost, 127.0.0.1) in the same middleware. This closes the cookie-CSRF hole the
   original design reintroduced.
3. **iOS storage containers.** Safari, the installed PWA, and in-app browsers have mutually
   isolated storage. Pairing must be an in-app screen (any 401 → "scan to pair" inside the
   app's own container), and subresource auth (PDF link-out, figures) uses short-lived signed
   URLs (`?st=<hmac, 5-min>`) instead of cookies crossing containers. Also handles iOS's
   ~7-day localStorage eviction: re-pairing is one scan, with an explicit "session expired"
   state instead of silent 401s.
4. **Host availability is named, not assumed.** The daemon, Tailscale, and the Lambda SSH
   tunnels all die when the laptop lid closes — exactly when the supervisor is away.
   MOBILE_ACCESS.md documents `caffeinate -s`/`pmset` (or running the daemon on an always-on
   box — it is already host-portable). The UI ships a global "daemon unreachable since <t>"
   freshness state distinct from "you're off-tailnet": for a situational-awareness tool,
   silently stale is worse than down.
5. **Battery is radio wakeups, not bytes.** Adaptive cadence: 5s only when something is live
   (running experiment / terminal segment open), decaying to 30–60s when the Now screen shows
   "nothing needs you"; pull-to-refresh as instant override; the 3 per-tick GETs batched onto
   one timer so the radio wakes once per cycle.

## 4. Phases (re-baselined: ~26 engineer-days core)

The original 20-day estimate did not absorb the judges' grafts or the red-team fixes; this is
the honest baseline. Each phase ships standalone value. Pre-agreed cut list if time presses:
filter chips, InfoDisclosure long tail, VisualDag node-list fallback — drop without re-planning.
Descope Phase 3 before trimming Phase 0 security.

### Phase 0 — Reachable, served, authenticated (5 days, backend + ops)

Justified on security grounds alone, independent of mobile.

- `tailscale serve --bg https / http://127.0.0.1:8787` fronts the loopback-only daemon →
  `https://<machine>.<tailnet>.ts.net` with a real cert. No open ports, no reverse proxy to
  build. HTTPS unlocks clipboard, PWA install, and (later) Web Push.
- NEW backend: StaticFiles mount for `research_state_ui/dist` + SPA `index.html` fallback
  (exempting `/api/*`, `/mcp/*`, `/health`), behind `--ui-dist` so the Vite dev loop is
  unchanged. Default-deny bearer-token middleware per §3.1–3.2; token generated at startup,
  persisted in `.research_plugin/daemon.json`, printed once. CORS tightened from `*`.
- Client: `api.js request()` attaches the token from localStorage; in-app pairing screen.
- Ops: `scripts/mobile_access.py` — preflights `tailscale status`, MagicDNS/cert capability,
  force-mints the cert *before* printing a terminal QR (URL + fragment token, parse-then-strip
  so it never hits logs); `rotate-token` CLI. `docs/MOBILE_ACCESS.md` incl. the lid-close
  caveat and the v1 stance: mobile supports exactly one daemon (the default :8787).
- **Value shipped:** destructive endpoints no longer drive-by-callable from any browser
  (a present-tense desktop vulnerability, fixed); UI served by the daemon with deep links;
  emergency pinch-zoom access from the phone.

### Phase 0.5 — Notifier (1.5 days, day ~6)

Grafted from the product-first design; the most shippable notification idea in the field and
it needs zero client code, so it ships *before* the UI work, not after.

- Daemon-side observer on the workflow/event layer → **ntfy (or Telegram)** push on:
  gate-needs-review, experiment failed, sandbox expiring <30 min, sync error. Dedup +
  rate-limit; access-token-protected topic; deep links to `/experiments/:id` (work because
  Phase 0 shipped the SPA fallback). Web Push stays a documented later upgrade.
- If the notifier runs on the same laptop, document that alerts share the host's fate.
- **Value shipped:** job 5 (get pulled in) at day six. "Sandbox expiring" reaches the phone
  even with no mobile UI built yet.

### Phase 1 — Mobile shell + Now screen (7 days)

- **First:** `manualChunks` + route-level `React.lazy` splitting @xyflow/react (~150 KB) and
  prism out of the 782 KB monolith; smoke desktop immediately (ExperimentGraphs' MeasureSync
  setTimeout hack may encode load-order assumptions). Budget stated as *JS fetched before Now
  is interactive* < 300 KB — ReactFlow must not load on `/`.
- `useViewport` (capability + min-dimension gate, `rsui:surface` override); `MobileShell`
  (app bar with project name + freshness dot + theme toggle; bottom nav Now / Experiments /
  Activity / More; More sheet hosts the remaining routes, ProjectSwitcher, settings).
- **NowScreen** — the product. One needs-attention stack from existing selectors, nothing new
  server-side: gate cards (GateBanner data, verbatim), open review requests, running
  experiments (FSMStrip + StatusPill), sandbox burn strip (soonest-expiring first), sync
  errors, freshness, last ~10 events. Empty state = "nothing needs you" — a successful glance.
- `ExperimentCardList` (replaces the 720px table): name, status, FSM strip, 2-line intent
  clamp with tap-disclosure, evidence glyphs, `fmtDayTime` inline. Filter chips by FSM state.
  Claims/Reviews card treatments (ReviewCard is already a card).
- Adaptive polling per §3.5; events capped at 100 on mobile; global unreachable/stale state.
- Tests where it counts: vitest specs for `terminalBlocks.js`, `planSections.js`,
  `evidence.js` (highest-blast-radius shared parsers in a zero-test repo — an afternoon).
  One Playwright two-viewport smoke so global.css edits provably don't move desktop pixels.
  Manual cross-viewport checklist committed to the repo.
- Record the frozen-interface contract in FRONTEND_REDESIGN_OBJECTIVES.md as a merge-gate
  item: `/home` payload shape, the terminal `tail`/`since` cursor, and named store selectors
  are cross-cutting review items for the desktop redesign.
- **Value shipped:** job 1 (glance) fully done; desktop untouched.

### Phase 2 — Watch a run + the one action (7 days)

- Mobile ExperimentDetail: segmented control — Status | Plan | Terminal | Metrics | Reviews —
  only the active segment mounts and polls (kills the 5-poller pile-up; figure/graph never
  poll concurrently). Plan = `planSections.js` + MarkdownView, reused unmodified.
- Terminal, hydration model: full-height 100dvh segment (no nested scroll), initial
  `tail=20000`, "load earlier" for history, collapsed `terminalBlocks` rendered as summary
  rows with bodies mounted on expand, blocks memoized by byte offset so a poll tick appends
  instead of re-parsing; 44px fold rows with explicit chevrons. Fix SandboxTerminal's missing
  visibilitychange pause (ships to desktop too — it's a live bug).
- **MetricsChart** (~150-line plain-SVG polyline, zero deps) over the archived MLflow metrics
  endpoint + live `sandbox/metrics` readout. **No iframes on mobile, period**: Lambda
  dashboards get an honest empty state ("runs over an SSH tunnel to your desktop — archived
  curves below"; a `/proxy/mlflow/{sandbox_id}` daemon route is a documented future upgrade,
  not "impossible"); Modal's public HTTPS tunnel URLs render as open-in-new-tab buttons.
- `SandboxCardList` (replaces the 840px table) with burn/expiry; **Release** via
  slide-to-confirm + running-experiment escalation per §2. ObjId tap-to-copy (clipboard now
  works — HTTPS) + `InfoDisclosure` primitive replacing `title=` tooltips on detail screens.
- **Value shipped:** jobs 2 and 4 — live tail, exit codes, GPU/CPU/RAM, metric curves without
  an iframe, and the ability to release a burning VM from a trailhead.

### Phase 3 — Graphs, resources, PWA polish (5 days)

- Logic graphs, two-tier: **GraphOutline** (plain DOM, depth-ordered node rows with
  evidence glyphs, tap → bottom-sheet detail) is the *default* mobile rendering — instant,
  no @xyflow in the critical path; "view as graph" lazy-loads ReactFlow into a 100dvh
  fullscreen overlay with touch pan/pinch. Exact inert-preview recipe (written down so it
  isn't re-derived on-device): all interaction props off **and** `preventScrolling={false}`
  **and** a transparent tap-capture overlay. Node tap → bottom sheet (fixes the invisible
  below-canvas panel). `useScrollLock` hook (position:fixed body technique) fixes the iOS
  scroll-lock leak for all overlays.
- Resources on mobile: FileTree mounted in-page as a collapsible panel above
  ResourceContentView, 44px rows (today it exists only in the desktop sidebar). PDFs:
  signed-URL link-out to the native viewer (no pdf.js).
- VisualDag: notice card + plain node list. The touch port (~2 days) waits until the desktop
  canvas redesign settles what it absorbs — building it now is the one place retrofit-now
  genuinely fights redesign-later.
- PWA: manifest, apple-touch-icon, `viewport-fit=cover`, service worker (static assets only).
  Badge hygiene: `clearAppBadge` on hide so a frozen count never lies; persistent badge waits
  for a real delivery channel.
- Remaining InfoDisclosure sweep (timestamps, claim confidence, hardware cells).
- **Value shipped:** job 3 (read) complete including graph stories; installs to Home Screen.

### Later (explicitly deferred)

- **Web Push** (4 days when wanted): pywebpush + VAPID in the daemon, subscriptions table,
  hooks where `services/workflow.py` writes gate transitions; replaces/augments ntfy.
- VisualDag touch port (post-redesign). MLflow live proxy route. ETag/304 on `/home` as the
  cellular relief valve once payload sizes are measured (split/lite endpoint as escalation).

## 5. Non-goals

No second app / React Native. No mobile mutations beyond sandbox release. No SSE/WebSocket
(cursored polling is sufficient at these rates). No offline data (static-asset caching only,
but unreachability is a *designed state*, per §3.4). No pdf.js. No tablet layouts (≥769px
gets desktop). No public-internet exposure, multi-user auth, or roles — single supervisor on
a tailnet is the threat model. No TS migration; tests limited to the three parser specs + one
Playwright smoke. No responsive-izing of desktop pages the redesign will replace.

## 6. Top risks

| Risk | Mitigation |
|---|---|
| Phase 0 auth breaks a local consumer (MCP proxy, scripts, tests) | Default-deny + same-commit consumer matrix + 401 surface test (§3.1) |
| Desktop redesign lands mid-flight, collides in global.css | Mobile styles confined to mobile.css; two mechanical global.css edits as one early PR; frozen-interface merge gate; Playwright two-viewport smoke |
| iOS storage isolation breaks pairing/PDF | In-app pairing on 401; signed URLs for subresources (§3.3) |
| Laptop sleeps → mobile goes dark when it matters | Documented (`caffeinate`/always-on host); "unreachable since <t>" UI state; notifier heartbeat if off-host |
| Code-split trips MeasureSync load-order assumptions | Split lands first in Phase 1 with immediate desktop smoke |
| Polling drains battery over VPN | Adaptive cadence + batched single-timer ticks (§3.5) |
| Estimate creep | Re-baselined at ~26d; pre-agreed cut list; cut Phase 3 scope before Phase 0 security |
