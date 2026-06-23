import { Link } from 'react-router-dom';
import { useProjectStore, projectPath } from '../store/useProjectStore';

/**
 * Shared graph-model helpers — normalize the figure and logic/synthesis graph
 * payloads into the GraphOutline / GraphCanvasOverlay model, and the bottom-
 * sheet detail renderers. Used by the experiment graph section and the project
 * synthesis card. docs/MOBILE_UX_REVIEW.md §4.1 / §4.2.
 */

// figure node status → small visual family (mirrors ExperimentFigure.statusClass).
function figStatusClass(node) {
  const s = String(node.status || '');
  if (node.type === 'review') {
    return { pass: 'done', needs_changes: 'revise', fail: 'failed', open: 'open' }[s] || 'neutral';
  }
  if (node.type === 'claim') {
    return {
      supported: 'done', weakened: 'revise', contradicted: 'failed',
      active: 'open', draft: 'neutral', abandoned: 'faded',
    }[s] || 'open';
  }
  return {
    pending: 'neutral', active: 'open', done: 'done', failed: 'failed',
    superseded: 'faded', abandoned: 'faded', none: 'neutral',
  }[s] || 'neutral';
}
const FIG_STATUS_COLOR = {
  done: 'var(--supports)', open: 'var(--active)', revise: 'var(--qualifies)',
  failed: 'var(--refutes)', faded: 'var(--faint)', neutral: 'var(--line-strong)',
};
const FIG_TYPE_GLYPH = {
  attempt: '◇', resource: '▤', resource_group: '▣', review: '☑',
  sandbox: '▶', conclusion: '∴', claim: '◎',
};
const LOGIC_KIND_COLORS = [
  'var(--active)', 'var(--supports)', 'var(--qualifies)', 'var(--refutes)', 'var(--mcp)', 'var(--ice)',
];

export function normalizeFigure(figure) {
  const nodes = (figure?.nodes || []).map(n => {
    const sc = figStatusClass(n);
    return {
      id: n.id, label: n.label, sublabel: n.sublabel || '',
      kindLabel: n.type, color: FIG_STATUS_COLOR[sc], glyph: FIG_TYPE_GLYPH[n.type] || '•',
      raw: n,
    };
  });
  const edges = (figure?.edges || []).map(e => ({ from: e.from, to: e.to, label: e.type }));
  return { nodes, edges };
}

export function normalizeLogic(graph) {
  const colors = new Map();
  for (const n of graph?.nodes || []) {
    const k = String(n.kind || '').trim();
    if (k && !colors.has(k)) colors.set(k, LOGIC_KIND_COLORS[colors.size % LOGIC_KIND_COLORS.length]);
  }
  const nodes = (graph?.nodes || []).map(n => {
    const k = String(n.kind || '').trim();
    return {
      id: n.id, label: n.label, sublabel: n.detail || '',
      kindLabel: k, color: colors.get(k) || 'var(--line-strong)',
      glyph: String(n.status || '') === 'dead_end' ? '·' : '◆',
      raw: n,
    };
  });
  const edges = (graph?.edges || []).map(e => ({ from: e.from, to: e.to, label: e.label }));
  return { nodes, edges };
}

export function EdgeList({ outgoing, labelById }) {
  if (!outgoing || !outgoing.length) return null;
  return (
    <div className="gnode-edges">
      <div className="gnode-edges-head">leads to</div>
      {outgoing.map((e, i) => (
        <div key={i} className="gnode-edge">
          <span className="gnode-edge-arrow" aria-hidden="true">→</span>
          {e.label && <span className="gnode-edge-label">{e.label}</span>}
          <span className="gnode-edge-target">{labelById[e.to] || e.to}</span>
        </div>
      ))}
    </div>
  );
}

// Resolved node refs (logic graph) — same taxonomy as LogicGraph.NodeRef.
export function LogicRef({ refString, resolution }) {
  const r = resolution || { resolved: false, type: 'unknown' };
  if (r.type === 'resource' && r.resolved) {
    return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, `/resources/${r.resource_id}`)}>{r.kind || 'resource'} · {r.title || r.path} →</Link>;
  }
  if (r.type === 'claim' && r.resolved) {
    return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, `/claims/${r.claim_id}`)}>claim · {r.statement} →</Link>;
  }
  if (r.type === 'experiment' && r.resolved) {
    return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, `/experiments/${r.experiment_id}`)}>experiment · {r.intent} →</Link>;
  }
  if (r.type === 'review' && r.resolved) {
    return <span className="gnode-ref gnode-ref--static">review · {String(r.role || '').replace(/_/g, ' ')} · {r.verdict}</span>;
  }
  return <span className="gnode-ref gnode-ref--unresolved">{refString}</span>;
}

// Figure ref → detail-page link, mirroring FigurePanel's destinations.
export function figureRefLink(node) {
  const ref = node.ref || {};
  if (ref.kind === 'resource' && ref.id) return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, `/resources/${ref.id}`)}>open resource →</Link>;
  if (ref.kind === 'resource_group') return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, '/resources')}>open resources →</Link>;
  if (ref.kind === 'claim' && ref.id) return <Link className="gnode-ref" to={projectPath(useProjectStore.getState().projectId, `/claims/${ref.id}`)}>open claim →</Link>;
  return null;
}

export function makeLogicDetail(refIndex) {
  return (node, ctx) => (
    <>
      <div className="gnode-meta">{String(node.kind || 'node').trim()}{node.status ? ` · ${node.status}` : ''}</div>
      {node.detail && <p className="gnode-detail">{node.detail}</p>}
      {Array.isArray(node.refs) && node.refs.filter(Boolean).map(r => (
        <LogicRef key={r} refString={r} resolution={refIndex[r]} />
      ))}
      <EdgeList outgoing={ctx.outgoing} labelById={ctx.labelById} />
    </>
  );
}

export function makeFigureDetail() {
  return (node, ctx) => (
    <>
      <div className="gnode-meta">{node.type}{node.status && node.status !== 'none' ? ` · ${node.status}` : ''}</div>
      {node.sublabel && <p className="gnode-detail">{node.sublabel}</p>}
      {figureRefLink(node)}
      <EdgeList outgoing={ctx.outgoing} labelById={ctx.labelById} />
    </>
  );
}
