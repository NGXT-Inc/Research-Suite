import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import { TERMINAL_STATUSES } from '../utils/experiment';
import GraphOutline from './GraphOutline';

const GraphCanvasOverlay = lazy(() => import('./GraphCanvasOverlay'));

// figure node status → the small visual family + color (mirrors
// ExperimentFigure.statusClass, kept local so this view owns no desktop code).
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

function normalizeFigure(figure) {
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

function normalizeLogic(graph) {
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

function EdgeList({ outgoing, labelById }) {
  if (!outgoing.length) return null;
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
function LogicRef({ refString, resolution }) {
  const r = resolution || { resolved: false, type: 'unknown' };
  if (r.type === 'resource' && r.resolved) {
    return <Link className="gnode-ref" to={`/resources/${r.resource_id}`}>{r.kind || 'resource'} · {r.title || r.path} →</Link>;
  }
  if (r.type === 'claim' && r.resolved) {
    return <Link className="gnode-ref" to={`/claims/${r.claim_id}`}>claim · {r.statement} →</Link>;
  }
  if (r.type === 'experiment' && r.resolved) {
    return <Link className="gnode-ref" to={`/experiments/${r.experiment_id}`}>experiment · {r.intent} →</Link>;
  }
  if (r.type === 'review' && r.resolved) {
    return <span className="gnode-ref gnode-ref--static">review · {String(r.role || '').replace(/_/g, ' ')} · {r.verdict}</span>;
  }
  return <span className="gnode-ref gnode-ref--unresolved">{refString}</span>;
}

// Figure ref → detail-page link, mirroring FigurePanel's destinations.
function figureRefLink(node) {
  const ref = node.ref || {};
  if (ref.kind === 'resource' && ref.id) return <Link className="gnode-ref" to={`/resources/${ref.id}`}>open resource →</Link>;
  if (ref.kind === 'resource_group') return <Link className="gnode-ref" to="/resources">open resources →</Link>;
  if (ref.kind === 'claim' && ref.id) return <Link className="gnode-ref" to={`/claims/${ref.id}`}>open claim →</Link>;
  return null;
}

/**
 * MobileGraphSection — the experiment's figure ⇄ logic graph on mobile.
 * Fetches both (single fetch on terminal experiments, slow poll while live),
 * renders the available one as a GraphOutline with a Figure/Logic toggle, and
 * offers "view as graph" → a lazy fullscreen ReactFlow overlay.
 */
export default function MobileGraphSection({ projectId, experimentId, experimentStatus, attemptIndex }) {
  const [figure, setFigure] = useState(null);
  const [logic, setLogic] = useState(null);
  const [chosen, setChosen] = useState('figure');
  const [showCanvas, setShowCanvas] = useState(false);

  const fetchBoth = useCallback(async () => {
    const [fig, lg] = await Promise.allSettled([
      api.getExperimentFigure(projectId, experimentId),
      api.getExperimentLogicGraph(projectId, experimentId),
    ]);
    if (fig.status === 'fulfilled') setFigure(fig.value);
    if (lg.status === 'fulfilled') setLogic(lg.value);
  }, [projectId, experimentId]);

  useEffect(() => {
    fetchBoth();
    if (TERMINAL_STATUSES.includes(experimentStatus)) return undefined;
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchBoth();
    }, 5000);
    return () => clearInterval(t);
  }, [fetchBoth, experimentStatus, attemptIndex]);

  const figModel = useMemo(() => normalizeFigure(figure), [figure]);
  const logicGraph = logic?.available ? logic.graph : null;
  const logicModel = useMemo(() => normalizeLogic(logicGraph), [logicGraph]);
  const refIndex = logic?.ref_index || {};

  const figAvail = figModel.nodes.length >= 2;
  const logicAvail = logicModel.nodes.length >= 1;

  // Resolve which view to show: honor the toggle, fall back to the other.
  const view = (chosen === 'figure' && figAvail) || (chosen === 'logic' && logicAvail)
    ? chosen
    : (figAvail ? 'figure' : (logicAvail ? 'logic' : null));

  const model = view === 'logic' ? logicModel : figModel;

  const renderDetail = view === 'logic'
    ? (node, ctx) => (
        <>
          <div className="gnode-meta">{String(node.kind || 'node').trim()}{node.status ? ` · ${node.status}` : ''}</div>
          {node.detail && <p className="gnode-detail">{node.detail}</p>}
          {Array.isArray(node.refs) && node.refs.filter(Boolean).map(r => (
            <LogicRef key={r} refString={r} resolution={refIndex[r]} />
          ))}
          <EdgeList outgoing={ctx.outgoing} labelById={ctx.labelById} />
        </>
      )
    : (node, ctx) => (
        <>
          <div className="gnode-meta">{node.type}{node.status && node.status !== 'none' ? ` · ${node.status}` : ''}</div>
          {node.sublabel && <p className="gnode-detail">{node.sublabel}</p>}
          {figureRefLink(node)}
          <EdgeList outgoing={ctx.outgoing} labelById={ctx.labelById} />
        </>
      );

  return (
    <section className="section">
      <div className="cluster--between" style={{ marginBottom: 10 }}>
        <div className="mseg mseg--inline" role="tablist" aria-label="Graph view">
          <button
            type="button"
            role="tab"
            aria-selected={view === 'figure'}
            className={`mseg-btn${view === 'figure' ? ' active' : ''}`}
            disabled={!figAvail}
            onClick={() => setChosen('figure')}
          >
            Figure
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'logic'}
            className={`mseg-btn${view === 'logic' ? ' active' : ''}`}
            disabled={!logicAvail}
            onClick={() => setChosen('logic')}
          >
            Story
          </button>
        </div>
        {view && model.nodes.length > 0 && (
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => setShowCanvas(true)}>
            View as graph ⤢
          </button>
        )}
      </div>

      {!view ? (
        <div className="empty-state empty-state--compact">
          <p>No figure or story graph yet — the figure derives from experiment state, and the agent authors the story as the run progresses.</p>
        </div>
      ) : (
        <GraphOutline nodes={model.nodes} edges={model.edges} renderDetail={renderDetail} />
      )}

      {showCanvas && view && (
        <Suspense fallback={<div className="gcanvas-overlay gcanvas-overlay--loading">Loading graph…</div>}>
          <GraphCanvasOverlay
            title={view === 'logic' ? (logicGraph?.title || 'Story graph') : 'Figure'}
            nodes={model.nodes}
            edges={model.edges}
            onClose={() => setShowCanvas(false)}
          />
        </Suspense>
      )}
    </section>
  );
}
