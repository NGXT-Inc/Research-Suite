import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ReactFlow, Background, Controls, Handle, Position, MarkerType } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { api } from '../api';
import { MeasureSync } from './ExperimentFigure';
import DetailPanelShell from './DetailPanelShell';
import { layoutFigure, FIG_NODE_W } from '../utils/figureLayout';
import { TERMINAL_STATUSES } from '../utils/experiment';
import { usePanelWidth } from '../store/usePanelWidth';

// Node `kind` is the agent's own vocabulary — there is no fixed taxonomy, so
// each kind gets an accent color by order of first appearance, used as the
// node's left border (each node also prints its kind as text).
const KIND_COLORS = [
  'var(--active)',
  'var(--supports)',
  'var(--qualifies)',
  'var(--refutes)',
  'var(--mcp)',
  'var(--ice)',
];
const NEUTRAL_COLOR = 'var(--line-strong)';

function kindColorMap(graph) {
  const colors = new Map();
  for (const node of graph?.nodes || []) {
    const kind = String(node.kind || '').trim();
    if (kind && !colors.has(kind)) {
      colors.set(kind, KIND_COLORS[colors.size % KIND_COLORS.length]);
    }
  }
  return colors;
}

function LogicNode({ data, selected }) {
  return (
    <div
      className={[
        'fig-node',
        data.dead ? 'lgr-node--dead' : '',
        selected ? 'fig-node--selected' : '',
      ].filter(Boolean).join(' ')}
      style={{ width: FIG_NODE_W, borderLeftColor: data.color }}
    >
      <Handle type="target" position={Position.Left} className="fig-handle" />
      {data.kind && (
        <div className="fig-node-head">
          <span className="fig-node-type">{data.kind}</span>
        </div>
      )}
      <div className="fig-node-label" title={data.label}>{data.label}</div>
      {data.detail ? <div className="fig-node-sub" title={data.detail}>{data.detail}</div> : null}
      <Handle type="source" position={Position.Right} className="fig-handle" />
    </div>
  );
}

const nodeTypes = { logic: LogicNode };

function toFlow(graph) {
  // Render only well-formed nodes (object, non-empty string id, first
  // occurrence wins on duplicates). The server lint reports the malformed
  // ones; react-flow must never see an undefined or repeated node id.
  const seen = new Set();
  const safeNodes = (Array.isArray(graph?.nodes) ? graph.nodes : []).filter(n => {
    if (!n || typeof n !== 'object' || typeof n.id !== 'string' || !n.id) return false;
    if (seen.has(n.id)) return false;
    seen.add(n.id);
    return true;
  });
  if (!safeNodes.length) {
    return { nodes: [], edges: [] };
  }
  const colors = kindColorMap(graph);
  const ids = new Set(safeNodes.map(n => n.id));
  const rawEdges = (Array.isArray(graph.edges) ? graph.edges : [])
    .filter(e => e && ids.has(e.from) && ids.has(e.to) && e.from !== e.to)
    .map((e, i) => ({ ...e, id: `${e.from}->${e.to}:${i}` }));
  const laid = layoutFigure({ nodes: safeNodes, edges: rawEdges });
  const nodes = laid.nodes.map(n => ({
    id: n.id,
    type: 'logic',
    position: { x: n.x, y: n.y },
    data: {
      ...n,
      kind: String(n.kind || '').trim(),
      color: colors.get(String(n.kind || '').trim()) || NEUTRAL_COLOR,
      dead: String(n.status || '') === 'dead_end',
    },
    draggable: false,
    connectable: false,
  }));
  const edges = laid.edges.map(e => ({
    id: e.id,
    source: e.from,
    target: e.to,
    type: 'smoothstep',
    className: 'fig-edge',
    label: e.label || undefined,
    markerEnd: { type: MarkerType.ArrowClosed, width: 13, height: 13 },
  }));
  return { nodes, edges };
}

/**
 * One node ref, rendered from the server's read-time resolution (ref_index).
 * Resolved refs link to the record they name; unresolved ones degrade to
 * gray text — refs are the agent's free-form pointers, never an error.
 */
function NodeRef({ refString, resolution }) {
  const r = resolution || { resolved: false, type: 'unknown' };
  if (r.type === 'resource' && r.resolved) {
    return (
      <Link className="lgr-ref" to={`/resources/${r.resource_id}`}>
        <span className="fig-node-type">{r.kind || 'resource'}</span> {r.title || r.path} →
      </Link>
    );
  }
  if (r.type === 'claim' && r.resolved) {
    return (
      <Link className="lgr-ref" to={`/claims/${r.claim_id}`}>
        <span className="fig-node-type">claim</span> {r.statement} →
      </Link>
    );
  }
  if (r.type === 'experiment' && r.resolved) {
    return (
      <Link className="lgr-ref" to={`/experiments/${r.experiment_id}`}>
        <span className="fig-node-type">experiment</span> {r.intent} →
      </Link>
    );
  }
  if (r.type === 'review' && r.resolved) {
    return (
      <span className="lgr-ref lgr-ref--static">
        <span className="fig-node-type">review</span> {String(r.role || '').replace(/_/g, ' ')} · {r.verdict}
      </span>
    );
  }
  if (r.type === 'synthesis' && r.resolved) {
    // No synthesis detail page yet; render the wave as a static chip.
    return (
      <span className="lgr-ref lgr-ref--static">
        <span className="fig-node-type">synthesis</span> {r.title || r.synthesis_id} · {r.status}
      </span>
    );
  }
  return (
    <span className="lgr-ref lgr-ref--unresolved" title={r.hint || 'not resolvable in this project'}>
      {refString}
    </span>
  );
}

function LogicPanel({ node, refIndex, onClose }) {
  const refs = Array.isArray(node.refs) ? node.refs.filter(r => typeof r === 'string' && r) : [];
  return (
    <DetailPanelShell typeLabel={node.kind || 'node'} title={node.label} onClose={onClose}>
      {node.status ? <div className="fig-panel-meta">status: {String(node.status)}</div> : null}
      {node.detail ? <div className="fig-panel-notes">{node.detail}</div> : null}
      {refs.length > 0 && (
        <div className="lgr-refs">
          {refs.map(r => <NodeRef key={r} refString={r} resolution={refIndex?.[r]} />)}
        </div>
      )}
    </DetailPanelShell>
  );
}

/**
 * LogicGraph — the agent-authored story of the experiment (role 'graph').
 *
 * Renders GET /experiments/{id}/graph: the decisions, problems, pivots, and
 * lessons the agent chose to record, as a small DAG (16-node budget). The
 * agent designs the graph — kinds, edge labels, and structure are its own
 * vocabulary, with kind accent colors derived from the data rather than a
 * fixed taxonomy. Polls while the experiment is live so the story grows on screen.
 *
 * Shares the canvas slot with ExperimentFigure via ExperimentGraphs:
 * `active` decides whether this view renders, `headerExtra` carries the
 * shared view switch, and `onAvailability` tells the parent whether there
 * is a story to show.
 */
export default function LogicGraph({
  projectId, experimentId, experimentStatus, attemptIndex,
  active = true, titleTabs = null, onAvailability = null,
  expanded = false, onToggleExpand = null,
  // Reuse hooks: the project-level synthesis panel renders the SAME component
  // against the project graph endpoint. `fetcher` overrides the data source,
  // `live` overrides the keep-polling decision, and the two text props swap
  // the experiment phrasing for project phrasing.
  fetcher = null,
  live = null,
  storyHint = "the experiment's story, told by the agent · click a node for detail",
  problemsGate = 'submit_results',
}) {
  // Same identity trick as ExperimentFigure: keep the payload as a JSON
  // string so unchanged polls never recreate node objects (react-flow keys
  // its measurements to object identity).
  const [payloadJson, setPayloadJson] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const rfRef = useRef(null);
  const { width: panelWidth, startResize } = usePanelWidth();

  const fetchGraph = useCallback(async () => {
    try {
      const data = fetcher
        ? await fetcher()
        : await api.getExperimentLogicGraph(projectId, experimentId);
      const json = JSON.stringify(data);
      setPayloadJson(prev => (prev === json ? prev : json));
    } catch {
      // Non-fatal: keep the last good payload. A transient fetch failure
      // (poll race with sandbox sync, daemon restart) must not blank the
      // story or flip the canvas back to the figure view for one tick.
    }
  }, [projectId, experimentId, fetcher]);

  const keepPolling = live != null ? live : !TERMINAL_STATUSES.includes(experimentStatus);
  useEffect(() => {
    fetchGraph();
    if (!keepPolling) return undefined;
    const t = setInterval(fetchGraph, 3000);
    return () => clearInterval(t);
  }, [fetchGraph, keepPolling, attemptIndex]);

  const payload = useMemo(() => (payloadJson ? JSON.parse(payloadJson) : null), [payloadJson]);
  const graph = payload?.available ? payload.graph : null;
  const { nodes, edges } = useMemo(() => toFlow(graph), [graph]);

  const topologyKey = useMemo(() => nodes.map(n => n.id).sort().join('|'), [nodes]);
  // Expanded mode may zoom past 1x so the graph actually uses the space.
  const fitMaxZoom = expanded ? 1.6 : 1;
  useEffect(() => {
    const t = setTimeout(() => rfRef.current?.fitView({ padding: 0.18, maxZoom: fitMaxZoom }), 350);
    return () => clearTimeout(t);
  }, [topologyKey, fitMaxZoom]);

  const selected = useMemo(
    () => (graph?.nodes || []).find(n => n.id === selectedId) || null,
    [graph, selectedId],
  );

  const hasStory = Boolean(graph && nodes.length);
  // A graph resource exists but nothing is drawable (unparseable JSON, empty
  // or malformed nodes): stay visible and surface the lint problems instead
  // of silently disabling the tab as if no graph had been written.
  const broken = Boolean(payload?.available && !hasStory);
  // Degraded re-associate case: a graph WAS associated yet its bytes were
  // never submitted, so the server returns available:false WITH problems.
  // Staying visible (rather than returning null) is the difference between
  // "no graph" and "graph needs re-associating" — surface the latter.
  const needsResubmit = Boolean(payload?.available === false && (payload?.problems?.length > 0));
  const available = hasStory || broken || needsResubmit;
  useEffect(() => { onAvailability?.(available); }, [available, onAvailability]);

  // Refit after the canvas resizes between inline and expanded modes.
  useEffect(() => {
    const maxZoom = expanded ? 1.6 : 1;
    const t = setTimeout(() => rfRef.current?.fitView({ padding: 0.18, maxZoom }), 120);
    return () => clearTimeout(t);
  }, [expanded]);

  if (!available || !active) return null;

  const maxNodes = payload?.max_nodes || 16;
  const problems = payload?.problems || [];

  return (
    <section className={`exp-figure${expanded ? ' exp-figure--expanded' : ''}`} id="logic-graph">
      <div className="fig-head">
        <div className="fig-title">
          {titleTabs || (graph?.title || 'Logic graph')}
          <span className="fig-title-hint">
            {titleTabs && graph?.title ? `${graph.title} · ` : ''}
            {storyHint}
          </span>
        </div>
        <div className="fig-head-right">
          <span className="lgr-badge">{(graph?.nodes || []).length} / {maxNodes} nodes</span>
          {onToggleExpand && (
            <button
              type="button"
              className="fig-expand-btn"
              onClick={onToggleExpand}
              aria-label={expanded ? 'Collapse graph' : 'Expand graph'}
            >
              {expanded ? '✕ Close' : '⤢ Expand'}
            </button>
          )}
        </div>
      </div>
      {problems.length > 0 && !needsResubmit && (
        <div className="lgr-problems">
          graph has envelope problems — the agent must fix these before {problemsGate}: {problems.join('; ')}
        </div>
      )}
      {needsResubmit && (
        <div className="lgr-broken">
          The graph file{payload?.path ? ` (${payload.path})` : ''} was associated
          but has no submitted content — re-associate it (role 'graph') to render
          the story here.
        </div>
      )}
      {broken && (
        <div className="lgr-broken">
          The graph file{payload?.path ? ` (${payload.path})` : ''} exists but
          cannot be drawn yet — once the problems above are fixed and the file
          is synced, the story renders here.
        </div>
      )}
      {hasStory && (
      <div
        className={`fig-body${selected ? ' fig-body--split' : ''}`}
        style={{ '--fig-panel-w': `${panelWidth}px` }}
      >
        <div className="fig-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onInit={inst => { rfRef.current = inst; }}
            onNodeClick={(event, node) => {
              event.stopPropagation();
              setSelectedId(node.id);
            }}
            onPaneClick={() => setSelectedId(null)}
            fitView
            proOptions={{ hideAttribution: true }}
            nodesDraggable={false}
            nodesConnectable={false}
            edgesFocusable={false}
            zoomOnDoubleClick={false}
            zoomOnScroll={expanded}
            zoomOnPinch
            preventScrolling={expanded}
            minZoom={0.3}
            maxZoom={1.6}
          >
            <MeasureSync topologyKey={topologyKey} />
            <Background gap={22} size={1.1} />
            <Controls showInteractive={false} position="bottom-right" />
          </ReactFlow>
          <div className="fig-canvas-hint">drag to pan · pinch to zoom</div>
        </div>
        {selected && (
          <div
            className="fig-resizer"
            onPointerDown={startResize}
            role="separator"
            aria-orientation="vertical"
            aria-label="Drag to resize panel"
          />
        )}
        {selected && (
          <LogicPanel
            node={selected}
            refIndex={payload?.ref_index}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
      )}
    </section>
  );
}
