import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow, ReactFlowProvider, Handle, Position, ViewportPortal,
  useReactFlow, useStore, useViewport,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { MeasureSync } from '../components/ExperimentFigure';
import { useMapModel } from './useMapModel';
import { CARD_W, CARD_H } from './mapLayout';
import MapPanel from './MapPanel';
import './expmap.css';

const SAT_ICON = { paper: '¶', claim: '✦', sbx: '▣' };
const PANEL_W = 380;
// Satellite wrap geometry: rows below the card, each within its width.
// Budget fits two max-width chips per row: 6 + 2×(20×6.2 + 34) = 322 ≤ 328
// (labels are capped at 19 kept chars + '…' in useMapModel's satTrunc).
const SAT_ROW_W = CARD_W + 44;
const SAT_ROWS = 2;
const SAT_ROW_H = 26;
const satW = (label) => label.length * 6.2 + 34;
const zoomBelowHalf = (s) => s.transform[2] < 0.5;

// Camera animations collapse to 0ms under prefers-reduced-motion.
function motionMs(ms) {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : ms;
}

// Selection/hover reach the nodes via context, NOT node data: recreating
// node objects on every selection would wipe react-flow's measured handle
// bounds and silently drop every edge (same gotcha as ExperimentFigure).
const MapCtx = createContext(null);

// Prototype fitAll frame: padded bounds (bounds already include card extents),
// scale capped at 1.15, vertically centered below the axis strip. Pure data —
// usable both as the initial viewport and for the fit button.
function fitViewportFor(bounds, vw, vh) {
  const minX = bounds.minX - 280;
  const maxX = bounds.maxX + 60;
  const minY = bounds.minY - 130;
  const maxY = bounds.maxY + 138;
  const s = Math.min(1.15, (vw - 80) / (maxX - minX), (vh - 150) / (maxY - minY));
  return { x: (vw - (minX + maxX) * s) / 2, y: (vh + 26 - (minY + maxY) * s) / 2, zoom: s };
}

// The canvas is finite: panning is clamped to the fitted frame plus a
// quarter-viewport of play on each side, and zooming out stops just past the
// fit — the experiments can never leave the screen.
function panBounds(bounds, vw, vh) {
  const f = fitViewportFor(bounds, vw, vh);
  const w = vw / f.zoom;
  const h = vh / f.zoom;
  const x0 = -f.x / f.zoom;
  const y0 = -f.y / f.zoom;
  return {
    minZoom: Math.max(0.05, f.zoom * 0.8),
    extent: [[x0 - w / 4, y0 - h / 4], [x0 + w * 1.25, y0 + h * 1.25]],
  };
}

// React Flow inline-styles pointer-events:none onto node wrappers unless nodes
// are draggable/selectable or a node click handler exists; our nodes are
// neither, so this noop is what keeps cards, satellites, and hover clickable.
const noopNodeClick = () => {};

// Suppress the click that follows a >4px drag (a pan that started on a node).
function useDragGuard() {
  const down = useRef(null);
  const onPointerDownCapture = (e) => { down.current = { x: e.clientX, y: e.clientY }; };
  const guard = (fn) => (e) => {
    e.stopPropagation();
    const d = down.current;
    if (d && Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 4) return;
    fn(e);
  };
  return { onPointerDownCapture, guard };
}

/**
 * One experiment on the map. Renders both densities and swaps at zoom < 0.5:
 * the full card (when/status, title, tldr + satellite pills below) or the
 * compact chip. Handles are invisible anchors only — edge geometry is
 * computed from layout.pos, not from measured handle bounds.
 */
function ExpNode({ data }) {
  const compact = useStore(zoomBelowHalf);
  const { card } = data;
  const { sel, selectExp, selectObject, setHover, clearHover } = useContext(MapCtx);
  const selected = sel?.type === 'exp' && sel.id === card.id;
  const selObj = sel && sel.type !== 'exp' ? `${sel.type}:${sel.id}` : null;
  const { onPointerDownCapture, guard } = useDragGuard();
  const onSelect = () => selectExp(card.id);
  const onHover = () => setHover(card.id);
  const onUnhover = () => clearHover(card.id);
  const onKey = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(); }
  };

  // Satellite pills wrap around the card instead of running off its right
  // edge: up to two rows below it, each kept within the card's width; what
  // doesn't fit folds into a '+N' chip that opens the panel, where the full
  // reference list lives. Advance uses the prototype's label-width formula.
  const sats = [];
  let satOverflow = 0;
  let satFold = null;
  {
    const all = card.sats || [];
    let row = 0;
    let sx = 6;
    for (let i = 0; i < all.length; i++) {
      const w = satW(all[i].label);
      let nr = row;
      let nx = sx;
      if (nx + w > SAT_ROW_W) { nr += 1; nx = 6; }
      if (nr >= SAT_ROWS) { satOverflow = all.length - i; break; }
      row = nr;
      sats.push({ ...all[i], left: nx, top: row * SAT_ROW_H });
      sx = nx + w;
    }
    if (satOverflow) {
      // The fold chip must land on the last row — reclaim the tail slot if full.
      if (sx + 46 > SAT_ROW_W && sats.length) {
        const popped = sats.pop();
        satOverflow += 1;
        sx = popped.left;
        row = popped.top / SAT_ROW_H;
      }
      satFold = { left: sx, top: row * SAT_ROW_H };
    }
  }

  const dotPulse = card.status === 'running' ? ' xmap-dot--pulse' : '';
  return (
    <div className="xmap-node" onPointerDownCapture={onPointerDownCapture}>
      <Handle type="source" position={Position.Left} className="xmap-handle" isConnectable={false} />
      <Handle type="target" position={Position.Right} className="xmap-handle" isConnectable={false} />
      {compact ? (
        <div
          className={`xmap-chip${selected ? ' xmap-chip--sel' : ''}`}
          role="button"
          tabIndex={0}
          aria-label={`${card.title} — ${card.status}`}
          onClick={guard(onSelect)}
          onKeyDown={onKey}
          onPointerEnter={onHover}
          onPointerLeave={onUnhover}
        >
          <span className={`xmap-dot xmap-dot--lg xmap-tone--${card.status}${dotPulse}`} />
          <span className="xmap-chip-title">{card.title}</span>
        </div>
      ) : (
        <>
          <div
            className={`xmap-card${selected ? ' xmap-card--sel' : ''}`}
            role="button"
            tabIndex={0}
            aria-label={`${card.title} — ${card.status}`}
            onClick={guard(onSelect)}
            onKeyDown={onKey}
            onPointerEnter={onHover}
            onPointerLeave={onUnhover}
          >
            <div className="xmap-card-head">
              <span className="xmap-when">{card.when}</span>
              <span className={`xmap-status xmap-tone--${card.status}`}>
                <span className={`xmap-dot${dotPulse}`} />
                {card.status}
              </span>
            </div>
            <div className="xmap-card-title">{card.title}</div>
            <div className="xmap-tldr">
              <span className="xmap-tldr-kind">{card.tldrKind}</span>
              {card.tldr}
            </div>
          </div>
          {sats.map((s) => (
            <button
              key={`${s.type}:${s.id}`}
              type="button"
              className={`xmap-sat${selObj === `${s.type}:${s.id}` ? ` xmap-sat--sel-${s.type}` : ''}`}
              style={{ left: s.left, top: CARD_H + 10 + s.top }}
              onClick={guard(() => selectObject(s.type, s.id))}
            >
              <span className={`xmap-sat-ic xmap-ic--${s.type}`} aria-hidden="true">{SAT_ICON[s.type] || '▣'}</span>
              <span className={`xmap-sat-label${s.type === 'sbx' ? ' xmap-sat-label--sbx' : ''}`}>{s.label}</span>
            </button>
          ))}
          {satOverflow > 0 && (
            <button
              type="button"
              className="xmap-sat"
              style={{ left: satFold.left, top: CARD_H + 10 + satFold.top }}
              aria-label={`${satOverflow} more references — open panel`}
              onClick={guard(onSelect)}
            >
              <span className="xmap-sat-label xmap-sat-label--sbx">+{satOverflow}</span>
            </button>
          )}
        </>
      )}
    </div>
  );
}

const nodeTypes = { exp: ExpNode };

/**
 * Reference edge — the exact prototype bezier from world anchors carried in
 * data. Handle-measured coordinates are ignored so card (right edge at
 * CARD_W, cy 61) and chip (300 / 22) anchors stay pixel-true across the
 * density swap. Solid = outgoing ref to the past, dashed = incoming citation.
 */
function XmapEdge({ data }) {
  const { x1, y1, x2, y2, op, dash } = data;
  const k = Math.max(40, Math.abs(x1 - x2) / 2);
  return (
    <g className="xmap-edge">
      <path
        d={`M ${x1} ${y1} C ${x1 - k} ${y1}, ${x2 + k} ${y2}, ${x2} ${y2}`}
        fill="none"
        strokeWidth={1}
        strokeLinecap="round"
        strokeOpacity={op}
        strokeDasharray={dash || undefined}
      />
      <circle cx={x2} cy={y2} r={2.5} fillOpacity={op + 0.1} />
      <circle cx={x1} cy={y1} r={2.5} fillOpacity={op + 0.1} />
    </g>
  );
}

const edgeTypes = { xmap: XmapEdge };

/**
 * Screen-space date strip pinned to the top of the map area. Labels sit at
 * tx + worldX·s and auto-drop when closer than 84px to the previous kept one;
 * the NOW label tracks the clamped now-line.
 */
function AxisStrip({ ticks, nowX }) {
  const { x: tx, zoom: s } = useViewport();
  const nowSx = Math.round(tx + nowX * s);
  // Experiment days place first (mutual 84px culling); margin days only fill
  // space no real label claimed — a virtual day must never displace a real one.
  const kept = [];
  let lastEnd = -Infinity;
  for (const t of ticks || []) {
    if (t.m) continue;
    // t.x carries the prototype's -24 world offset for the wide/tight label
    // proximity math; the render position restores it (prototype parity).
    const sxp = tx + (t.x + 24) * s;
    if (sxp - lastEnd < 84) continue;
    if (Math.abs(sxp - nowSx) < 70) continue; // leave the NOW label its room
    kept.push({ sx: Math.round(sxp), label: t.label });
    lastEnd = sxp + t.label.length * 4;
  }
  for (const t of ticks || []) {
    if (!t.m) continue;
    const sxp = tx + (t.x + 24) * s;
    if (Math.abs(sxp - nowSx) < 70) continue;
    if (kept.some((k) => Math.abs(sxp - k.sx) < 84)) continue;
    kept.push({ sx: Math.round(sxp), label: t.label });
  }
  kept.sort((a, b) => a.sx - b.sx);
  return (
    <div className="xmap-axis" aria-hidden="true">
      {kept.map((t) => (
        <span key={`${t.label}:${t.sx}`}>
          <span className="xmap-axis-label" style={{ left: t.sx }}>{t.label}</span>
          <span className="xmap-axis-tick" style={{ left: t.sx }} />
        </span>
      ))}
      <span className="xmap-axis-label xmap-axis-label--now" style={{ left: nowSx }}>now</span>
      <span className="xmap-axis-tick xmap-axis-tick--now" style={{ left: nowSx }} />
    </div>
  );
}

function Legend({ hasAbandoned }) {
  const entries = ['supports', 'qualifies', 'refutes', 'running', ...(hasAbandoned ? ['abandoned'] : [])];
  return (
    <div className="xmap-legend" aria-hidden="true">
      {entries.map((st) => (
        <span key={st} className={`xmap-legend-item xmap-tone--${st}`}>
          <span className="xmap-dot" />
          <span className="xmap-legend-word">{st}</span>
        </span>
      ))}
    </div>
  );
}

function MapCanvas({ model, wrapRef, size, initialViewport }) {
  const { cards, layout, objects, citedBy } = model;
  const rf = useReactFlow();
  const compact = useStore(zoomBelowHalf);
  const [sel, setSel] = useState(null);
  const [hover, setHover] = useState(null);

  // Artifact refs arrive as type 'art'; the object panel speaks 'sbx'.
  const selectObject = useCallback((type, id) => {
    setSel({ type: type === 'art' ? 'sbx' : type, id });
  }, []);

  // Transport: center the card in the viewport minus the panel, then select.
  const transportTo = useCallback((id) => {
    const p = layout.pos[id];
    const el = wrapRef.current;
    if (!p || !el) return;
    const s = Math.max(rf.getZoom(), 0.95);
    const vw = el.clientWidth - PANEL_W, vh = el.clientHeight;
    setSel({ type: 'exp', id });
    rf.setViewport(
      { x: vw / 2 - (p.x + CARD_W / 2) * s, y: vh / 2 - (p.y + CARD_H / 2) * s, zoom: s },
      { duration: motionMs(550) },
    );
  }, [layout, rf, wrapRef]);

  // Escape closes the panel.
  useEffect(() => {
    if (!sel) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setSel(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sel]);

  const selectExp = useCallback((id) => setSel({ type: 'exp', id }), []);
  const setHoverId = useCallback((id) => setHover(id), []);
  const clearHover = useCallback((id) => setHover((h) => (h === id ? null : h)), []);
  const ctx = useMemo(
    () => ({ sel, selectExp, selectObject, setHover: setHoverId, clearHover }),
    [sel, selectExp, selectObject, setHoverId, clearHover],
  );

  // Node objects stay identity-stable across selection/hover so react-flow
  // keeps its measured handle bounds (else edges silently vanish).
  const nodes = useMemo(() => cards.map((c) => ({
    id: c.id,
    type: 'exp',
    position: layout.pos[c.id] || { x: 0, y: 0 },
    draggable: false,
    connectable: false,
    selectable: false,
    data: { card: c },
  })), [cards, layout]);

  // Edges exist only for the selected and/or hovered experiment (prototype
  // edgesFor): solid outgoing refs to the past, dashed incoming citations.
  const edges = useMemo(() => {
    const out = [];
    const w = compact ? 300 : CARD_W;
    const cy = compact ? 22 : CARD_H / 2;
    const mk = (citingId, citedId, op, dash) => {
      const pa = layout.pos[citingId], pb = layout.pos[citedId];
      if (!pa || !pb) return;
      out.push({
        id: `e${out.length}:${citingId}->${citedId}`,
        source: citingId,
        target: citedId,
        type: 'xmap',
        data: { x1: pa.x, y1: pa.y + cy, x2: pb.x + w, y2: pb.y + cy, op, dash },
      });
    };
    const forExp = (id, op, opIn) => {
      const c = cards.find((x) => x.id === id);
      if (!c) return;
      for (const r of c.refs || []) if (r.type === 'exp') mk(id, r.id, op, null);
      for (const cid of citedBy[id] || []) mk(cid, id, opIn, '6 5');
    };
    if (sel?.type === 'exp') forExp(sel.id, 0.5, 0.22);
    if (hover && !(sel?.type === 'exp' && sel.id === hover)) forExp(hover, 0.25, 0.12);
    return out;
  }, [cards, layout, citedBy, sel, hover, compact]);

  const b = layout.bounds || { minX: 0, maxX: 0, minY: 0, maxY: 0 };

  // Finite canvas: clamp panning and outward zoom around the fitted frame.
  const pan = useMemo(
    () => (layout.bounds && size ? panBounds(layout.bounds, size.w, size.h) : null),
    [layout.bounds, size],
  );

  return (
    <MapCtx.Provider value={ctx}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        elementsSelectable={false}
        edgesFocusable={false}
        onNodeClick={noopNodeClick}
        panOnDrag
        zoomOnScroll
        zoomOnDoubleClick={false}
        minZoom={pan ? pan.minZoom : 0.25}
        maxZoom={2.5}
        translateExtent={pan ? pan.extent : undefined}
        defaultViewport={initialViewport}
      >
        {/* Keyed to the nodes array: any rebuilt node objects (poll ticks,
            progressive model fills) force a handle re-measure. */}
        <MeasureSync topologyKey={nodes} />
        <ViewportPortal>
          <div
            className="xmap-nowline"
            style={{ left: layout.nowX, top: b.minY - 800, height: (b.maxY - b.minY) + 2400 }}
          />
        </ViewportPortal>
      </ReactFlow>
      <AxisStrip ticks={layout.ticks} nowX={layout.nowX} />
      <Legend hasAbandoned={cards.some((c) => c.status === 'abandoned')} />
      {sel && (
        <MapPanel
          sel={sel}
          cards={cards}
          objects={objects}
          citedBy={citedBy}
          onClose={() => setSel(null)}
          onTransport={transportTo}
          onSelectObject={selectObject}
        />
      )}
    </MapCtx.Provider>
  );
}

/**
 * ExperimentMap — the Experiments page's map view. A single React Flow
 * canvas of experiment cards on a gap-compressed time axis, with reference
 * edges on hover/selection and a right-hand detail panel. All data and
 * layout come from useMapModel; this file is view-only.
 */
export default function ExperimentMap() {
  const wrapRef = useRef(null);
  // Measure the map area before mounting the canvas so the first frame IS the
  // fitted frame: the initial viewport comes from pure layout data instead of
  // an animated post-init fit, which node measurement can stall in hidden tabs.
  // The width also feeds the model so the world stretches to fill the pane.
  const [size, setSize] = useState(null);
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const measure = () => setSize((s) => {
      const w = el.clientWidth, h = el.clientHeight;
      return s && s.w === w && s.h === h ? s : { w, h };
    });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const model = useMapModel(size?.w);
  const empty = model.ready && (model.cards || []).length === 0;

  // Initial-only by design: React Flow applies defaultViewport once, so later
  // layout/resize changes never yank the camera out from under the user.
  // Gated on cards existing — a project whose first experiments arrive after
  // the map opens must frame them, not the empty-bounds origin.
  const hasCards = model.ready && model.cards.length > 0;
  const initialViewport = useMemo(
    () => (hasCards && size ? fitViewportFor(model.layout.bounds, size.w, size.h) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hasCards, size !== null],
  );

  return (
    <div className={`xmap${empty ? ' xmap--empty' : ''}`} ref={wrapRef}>
      {empty ? (
        <div className="empty-state"><h2>No experiments yet</h2></div>
      ) : model.ready && initialViewport ? (
        <ReactFlowProvider>
          <MapCanvas model={model} wrapRef={wrapRef} size={size} initialViewport={initialViewport} />
        </ReactFlowProvider>
      ) : null}
    </div>
  );
}
