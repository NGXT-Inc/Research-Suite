import { useMemo, useState } from 'react';
import { layoutFigure } from '../utils/figureLayout';
import BottomSheet from './BottomSheet';

/**
 * GraphOutline — the default mobile rendering of a small DAG (figure or logic
 * graph) as depth-ordered DOM rows instead of a touch-hostile canvas. No
 * @xyflow on the critical path: instant, scrollable, accessible. Tapping a row
 * opens a node-detail bottom sheet (fixes the desktop "invisible below-canvas
 * panel"). docs/MOBILE_UX_REVIEW.md §4.1.
 *
 * Model-agnostic: the caller normalizes both graph shapes into
 *   nodes: [{ id, label, sublabel?, kindLabel?, color, glyph?, raw }]
 *   edges: [{ from, to, label? }]
 * and supplies renderDetail(rawNode, { outgoing, labelById }) for the sheet.
 */
export default function GraphOutline({ nodes, edges, renderDetail }) {
  const [selId, setSelId] = useState(null);

  // Reuse the deterministic figure layout purely to get a reading order:
  // column (depth) gives flow left→right, y orders within a column.
  const ordered = useMemo(() => {
    const laid = layoutFigure({ nodes: nodes.map(n => ({ ...n })), edges });
    const xs = [...new Set(laid.nodes.map(n => Math.round(n.x)))].sort((a, b) => a - b);
    const depthOf = (x) => Math.max(0, xs.indexOf(Math.round(x)));
    const byId = new Map(nodes.map(n => [n.id, n]));
    return laid.nodes
      .map(ln => ({ ...byId.get(ln.id), depth: depthOf(ln.x), _y: ln.y }))
      .sort((a, b) => (a.depth - b.depth) || (a._y - b._y));
  }, [nodes, edges]);

  const outgoing = useMemo(() => {
    const m = new Map();
    for (const e of edges || []) {
      if (!m.has(e.from)) m.set(e.from, []);
      m.get(e.from).push(e);
    }
    return m;
  }, [edges]);

  const labelById = useMemo(
    () => Object.fromEntries(nodes.map(n => [n.id, n.label])),
    [nodes],
  );
  const selected = ordered.find(n => n.id === selId) || null;

  return (
    <div className="goutline">
      {ordered.map(n => (
        <button
          key={n.id}
          type="button"
          className="goutline-row"
          style={{ paddingLeft: 10 + Math.min(n.depth, 5) * 14 }}
          onClick={() => setSelId(n.id)}
        >
          <span className="goutline-rail" style={{ background: n.color }} aria-hidden="true" />
          <span className="goutline-main">
            <span className="goutline-line">
              {n.kindLabel && <span className="goutline-kind">{n.kindLabel}</span>}
              <span className="goutline-label">{n.label}</span>
              {n.glyph && <span className="goutline-glyph" style={{ color: n.color }} aria-hidden="true">{n.glyph}</span>}
            </span>
            {n.sublabel && <span className="goutline-sub">{n.sublabel}</span>}
          </span>
        </button>
      ))}

      <BottomSheet
        open={Boolean(selected)}
        onClose={() => setSelId(null)}
        label="Node detail"
        title={selected?.label}
      >
        {selected && renderDetail(selected.raw, {
          outgoing: outgoing.get(selected.id) || [],
          labelById,
        })}
      </BottomSheet>
    </div>
  );
}
