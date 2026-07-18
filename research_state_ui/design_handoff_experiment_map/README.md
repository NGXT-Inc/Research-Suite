# Handoff: Merv Experiment Map

## Overview
An infinite pan/zoom canvas that visualizes a research program as a temporal map of experiments. Each experiment is a card placed on a gap-compressed time axis; papers and claims it references hang beneath it as small satellite pills; selecting a card reveals its reference edges to other experiments and a detail panel. The map answers two questions at a glance: *what is this experiment in a nutshell* and *what does it connect to / where did the idea come from*.

Intended to be **programmatically generated**: the only inputs are the experiments' plans and reports (which contain references to other experiments, papers, and claims), timestamps/durations, statuses, gate results, and sandbox usage. Nothing on the map is hand-placed.

## About the Design Files
The files in this bundle are **design references created in HTML** — interactive prototypes showing intended look and behavior, not production code to copy directly. The task is to **recreate this design in the target codebase's existing environment** using its established patterns and libraries (e.g. React + React Flow or a custom canvas layer). If no frontend exists yet, choose the stack that best fits (React + a viewport/graph library is the natural fit given the react-flow-style edge handles). The `.dc.html` files use a proprietary streaming-template runtime (`support.js`) — read them for markup, styles, and logic; do not ship them.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and interactions are final intent. Recreate pixel-perfectly, adapting only to the product's real design system where it already defines equivalents. Two complete themes are provided (dark is primary; light is a full palette mapping, listed under Design Tokens).

## Data Model (what the map consumes)
```
Experiment {
  id            'exp-0473'
  title         short human title
  date          start datetime (ISO)
  dur           run duration ('36h') — absent/running if still in flight
  status        'supports' | 'qualifies' | 'refutes' | 'running'   // product's evidence triad + running
  plan          one-sentence TL;DR of the plan
  review        one-sentence TL;DR of the report/review (null while running)
  metrics       [{ value: '+2.3%', label: 'eval, best config' }]
  gates         [[label, result, tone]] e.g. ['adversarial review', 'passed 2/3', 'qualifies']
  refs          [['exp'|'paper'|'claim', id]]   // extracted from plan/report
  sbx           ['sbx-h1', 'sbx-h2']            // sandboxes used (shared, 0..n per experiment)
  compute       '36h × 16×H100'                 // spend summary, panel-only
  artifacts     count
  agent         'claude-code' | 'codex' | …
}
Paper   { title, sub ('Fedus et al. · 2021 · arXiv 2101.03961'), detail, short }
Claim   { title (the claim sentence), sub (provenance), detail, short }
Sandbox { title ('sbx-h1 · 8×H100'), sub, detail, short }
```

## Layout Algorithm (all programmatic)
1. **Gap-compressed time axis.** `x = f(start time)`, piecewise linear. Walk experiment start times in order at `HOUR = 6px/hour`; any gap `> 30h` collapses to a fixed `90px` segment. Extrapolate past the last event at the same 6px/h rate. This keeps the canvas dense — no dead space for idle periods.
2. **Row packing (y).** Sort experiments by x; each takes the first row (index r, `y = r × 215px`) whose last occupant's right edge is `> 28px` left of its x. No lanes, no manual positions.
3. **Now marker.** A 1px vertical line at `x = max(f(now), rightmostCardRightEdge + 48)` — **nothing may ever sit to its right**. Color `rgba(247,107,21,0.2)`, full canvas height.
4. **Satellites.** Paper/claim/sandbox pills sit in a row starting at `card.x + 6, card.y + cardHeight + 10`, advancing by `labelLength × 6.2 + 34px` each.

## Screens / Views

### 1. Canvas (main view)
Full-viewport, `background #131311` (warm near-black), `overflow hidden`. World layer transformed by `translate(tx,ty) scale(s)`, origin 0 0.

**Header overlay** (fixed, top): padding `20px 28px`, fade-out gradient from `#131311`. Left: 5×22px orange `#f76b15` bar (2px radius) + "Merv" (Inter 16px/700) + "experiment map" (JetBrains Mono 12px `#67645d`). Right: status legend — 6px dots + labels in mono 11px `#9a978f` for supports/qualifies/refutes/running.

**Date axis strip** (fixed, screen-space at top:60px, height 26px): hairline bottom border `#1e1d1a`. Day labels (mono 10px, uppercase, letter-spacing 0.12em, `#67645d`, centered on tick) with a 1×7px tick mark `#39382f` below. Labels are placed at `screenX = tx + worldX × s` and **auto-drop when closer than 84px** to the previous kept label; label text is `"Thu, Jul 2"` when roomy, `"Jul 2"` when tight. A `now` label + tick in `#f76b15` sits at the now-line's screen x.

**Zoom controls** (fixed, bottom-left 24px): vertical pill group, 34×32px buttons (`+`, `−`, `fit`), bg `#1a1a17`, hover `#232320`, 1px `#2b2b27` border, 6px radius.

### 2. Experiment card (full zoom, s ≥ 0.5)
- 284px wide, bg `#1a1a17`, radius 8px, ring `0 0 0 1px #2b2b27` (hover `#4a4a44`, selected `#f76b15`), padding `13px 15px`, column flex gap 7px.
- **Header row** (mono 11px): start → finish time, `"Jul 10 08:00 → Jul 11 20:00"` (`#9a978f`, 10.5px, nowrap); running shows `"Jul 17 11:00 → …"`. Right-aligned status: 6px dot + word in the status color; running dot pulses (opacity 1→0.35, 1.4s ease infinite).
- **Title**: Inter 14px/600, `#e8e6e1`, line-height 1.3.
- **TL;DR**: 12px/1.5 `#9a978f`, prefixed by a mono 9px uppercase micro-label (`#67645d`, letter-spacing 0.15em) reading `review` — or `plan` for running experiments. One TL;DR only: the review once it exists, else the plan.

### 3. Satellite pills (default-visible, de-emphasized)
Pill: mono 10px, bg `#161613`, 1px border `#26261f` (hover `#4a4a44`; when its object is selected, border takes the type color), radius 999px, padding `3px 9px 3px 7px`, gap 5px. Icon (9px) + short label:
- Paper `¶` icon `#74bf7c`, label `#8a877f` (e.g. "Switch Transformers")
- Claim `✦` icon `#d4a045`, label `#8a877f` (e.g. "sparse law")
- Sandbox `▣` icon `#55524b`, label `#67645d` (quietest — compute is a signal, never an emphasis)

### 4. Compact chip (density zoom, s < 0.5)
Cards swap to pills at the same x/y: bg `#1a1a17`, radius 999px, ring as cards, padding `11px 20px`, 11px status dot + title (Inter 20px/600 `#e8e6e1`, ellipsis at 430px). Satellites hidden. Same click/hover behavior.

### 5. Reference edges (SVG, world layer, pointer-events none)
Drawn only on hover (faint) or selection (stronger). React-flow-style fixed handles: an edge leaves the **citing (later) card's left-center** and enters the **cited (earlier) card's right-center** (`cy = cardH/2`, or 22px for chips). Cubic bezier `M x1 y1 C x1−k y1, x2+k y2, x2 y2` with `k = max(40, |x1−x2|/2)`. 2.5px connection dots at both ends.
- Stroke `#67645d`, width 1, round caps. **Solid = outgoing** (references to the past). **Dashed `6 5` = incoming** (citations from the future).
- Opacities: selected out 0.5 / in 0.22; hover-preview out 0.25 / in 0.12; dots +0.1.

### 6. Detail panel (right, on selection)
380px, full height, bg `#181815`, 1px left border `#2b2b27`, scrollable.
- **Sticky header**: exp id in mono 12px `#f76b15` (papers show their arXiv id; claims/sandboxes their id), status dot + word, ✕ close (`#67645d` → `#e8e6e1`).
- **Title** Inter 17px/700 + body 13px/1.6 `#9a978f` (review TL;DR, else plan; objects show their detail text).
- **Section eyebrows**: mono 10px uppercase `#f76b15`, letter-spacing 0.2em, 10px bottom margin.
- **Result**: metric chips — bg `#131311`, 1px `#2b2b27`, radius 6, padding `8px 12px`; value mono 14px `#e8e6e1`, label 10px `#67645d`.
- **Gates**: mono 12px rows, label `#9a978f`, dotted leader `rgba(255,255,255,0.14)`, result in its tone color.
- **References**: rows (bg `#131311`, 1px `#2b2b27`, radius 6, padding `10px 12px`, hover border `#f76b15`): type icon in type color, label 12px/500 `#e8e6e1`, sub 11px `#67645d`, right action mono 11px — `go →` for experiments, `view →` for objects.
- **Cited by** (only when non-empty): same row style, experiments that reference this one.
- **Footer** meta: mono 11px `#67645d` — `14 artifacts · claude-code · 36h × 16×H100 · sbx-h1 sbx-h2`.
- Object panels (paper/claim/sandbox) show title/detail + a **Referenced by** list (sandboxes: every experiment that used it).

## Interactions & Behavior
- **Pan**: pointer drag anywhere on canvas (cursor grab/grabbing). A drag of >4px suppresses the click that would otherwise fire on release.
- **Zoom**: wheel, toward cursor, `s ∈ [0.25, 2.5]`, factor `exp(−deltaY × 0.0012)`; wheel listener must be non-passive. Buttons step ×1.25. `fit` frames all content.
- **Select card** → orange ring, edges draw, panel opens.
- **Hover card/chip** → its edges pre-render at preview opacity (clears on leave).
- **Satellite / panel object row click** → object detail panel; row `stopPropagation`s so the card doesn't steal it.
- **Experiment ref click ("go →")** → *transport*: camera animates (550ms, ease-out cubic `1−(1−p)³`) to center that card in the viewport minus the 380px panel, at `scale ≥ 0.95`, and selects it.
- **fit / initial load**: animate to bounding box of all cards (padding ~280/130px, cap s ≤ 1.15), vertically centered below the header.
- **Density switch**: pure render swap at s = 0.5, no animation needed.
- Panel close (✕) deselects and removes edges.

## State Management
`{ s, tx, ty }` camera; `sel: { type: 'exp'|'paper'|'claim'|'sbx', id } | null`; `hover: expId | null`; `panning: bool`; one rAF-driven camera animation at a time (cancel previous). Layout (x/y per experiment, axis ticks, now-x) is derived data — compute once per dataset, not per frame.

## Design Tokens
**Dark (primary)** — bg `#131311` · surface `#1a1a17` · surface-2 `#161613` · panel `#181815` · line `#2b2b27` · line-faint `#26261f` / `#1e1d1a` · text `#e8e6e1` · muted `#9a978f` · faint `#67645d` · fainter `#55524b` · satellite-label `#8a877f` · hover-ring `#4a4a44` · brand `#f76b15` (hover `#ff8a3d`) · supports `#74bf7c` · qualifies `#d4a045` · refutes `#c25b4e` · running `#f76b15` · edge `#67645d` · leader `rgba(255,255,255,0.14)`

**Light** (same structure, warm paper): bg `#f5f4f0` · surface `#ffffff` · surface-2 `#f0efe9` · panel `#fbfaf7` · line `#e2e0d8` · axis line `#e6e4dc` · text `#26251f` · muted `#6e6b62` · faint `#97938a` · fainter `#a5a196` · satellite-label `#7a766c` · hover-ring `#c9c6bb` · brand `#f76b15` (hover `#d55708`) · supports `#3e8a4a` · qualifies `#9c7817` · refutes `#b0442f` · leader `rgba(0,0,0,0.18)`

**Type**: JetBrains Mono (ids, timestamps, statuses, eyebrows, metadata) + Inter (titles, body). Both via Google Fonts, weights 400–700.
**Spacing/radius**: cards 8px, rows/chips 6px, pills 999px; card padding 13/15, panel padding 20/22.

## Assets
None — no images or icon fonts. Icons are unicode glyphs: `¶` paper, `✦` claim, `▣` sandbox, `⧉` experiment ref, `✕` close, `▸/→/⋯` inline. Brand colors originate from the Merv landing (`merv` repo: orange `#f76b15`, status triad from the product's supports/qualifies palette, warm near-black `#131311` from `research_state_ui`).

## Files
- `Experiment Map.dc.html` — dark theme prototype (primary reference; template markup + full interaction logic in one file)
- `Experiment Map Light.dc.html` — light theme (identical structure, palette-swapped)
- `screenshots/01-dark-overview-density-zoom.png` — zoomed-out fit view: compact chips (s < 0.5), date strip, now marker
- `screenshots/02-dark-selection.png` — full cards with a selected experiment: orange ring, reference edges, satellite pills, detail panel
- `screenshots/03-light-overview.png` — light theme, zoomed-out fit view

Open either in a browser via the design tool to interact. All behavior described above is implemented in these files; when in doubt, the prototype is the spec.

## Notes for implementation
- Sample data (11 experiments, 5 papers, 4 claims, 5 sandboxes, July 2026) is embedded in the prototypes — replace with the real experiments/plans/reports pipeline. Reference extraction from plan/report text is the upstream contract; the map renders whatever refs arrive.
- `NOW` is a fixed constant in the prototype; use real wall-clock time in production (the clamp rule still applies).
- React Flow can supply the viewport/edge machinery, but note the custom requirements: gap-compressed x-axis, screen-space date strip synced to the viewport transform, collision row-packing, click-through transport animation, and the density-zoom card/chip swap.
