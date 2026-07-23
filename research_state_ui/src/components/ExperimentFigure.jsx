import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ReactFlow, Background, Controls, Handle, Position, MarkerType, useStoreApi } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { api } from '../api';
import StatusPill from './StatusPill';
import DetailPanelShell from './DetailPanelShell';
import ArtifactContentView from './ArtifactContentView';
import { layoutFigure, FIG_NODE_W } from '../utils/figureLayout';
import { TERMINAL_STATUSES } from '../utils/experiment';
import { usePanelWidth } from '../store/usePanelWidth';
import { useProjectHref } from '../store/useProjectStore';
import { useStreamAwarePoll } from '../store/useEventStream';

const TYPE_GLYPH = {
  attempt: '◇',
  artifact: '▤',
  artifact_group: '▣',
  review: '☑',
  sandbox: '▶',
  conclusion: '∴',
  claim: '◎',
};

/**
 * Normalize per-type statuses from the figure document into the small set of
 * visual states the CSS knows: done | open | revise | failed | faded | neutral.
 * (`open` = blue/in-motion, `revise` = amber, `faded` = superseded history.)
 */
function statusClass(node) {
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

function FigureNode({ data, selected }) {
  return (
    <div
      className={[
        'fig-node',
        `fig-node--${data.type}`,
        `fig-st--${data.statusClass}`,
        selected ? 'fig-node--selected' : '',
      ].filter(Boolean).join(' ')}
      style={{ width: FIG_NODE_W }}
    >
      <Handle type="target" position={Position.Left} className="fig-handle" />
      <div className="fig-node-head">
        <span className="fig-node-glyph" aria-hidden="true">{TYPE_GLYPH[data.type] || '•'}</span>
        <span className="fig-node-type">{data.type}</span>
        {data.statusClass === 'open' && <span className="fig-node-live" aria-hidden="true" />}
      </div>
      <div className="fig-node-label" title={data.label}>{data.label}</div>
      {data.sublabel ? <div className="fig-node-sub" title={data.sublabel}>{data.sublabel}</div> : null}
      <Handle type="source" position={Position.Right} className="fig-handle" />
    </div>
  );
}

const nodeTypes = { figure: FigureNode };

/**
 * Force a node re-measure after mount and on topology changes. Edge rendering
 * depends on measured handle bounds, which react-flow fills in via a
 * ResizeObserver + requestAnimationFrame pipeline — and browsers throttle
 * both to "never" in background tabs and headless previews, leaving every
 * edge silently unrendered. This dashboard is expected to live in background
 * tabs, so re-measure on plain timers (which do fire) by driving the store
 * action directly. Must live inside <ReactFlow> to reach its store context.
 */
export function MeasureSync({ topologyKey }) {
  const store = useStoreApi();
  useEffect(() => {
    const measure = () => {
      const { domNode, updateNodeInternals } = store.getState();
      const updates = new Map();
      domNode?.querySelectorAll('.react-flow__node[data-id]').forEach(el => {
        const id = el.getAttribute('data-id');
        updates.set(id, { id, nodeElement: el, force: true });
      });
      if (updates.size) updateNodeInternals(updates);
    };
    const t1 = setTimeout(measure, 0);
    const t2 = setTimeout(measure, 300);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [topologyKey, store]);
  return null;
}

function toFlow(figure) {
  const laid = layoutFigure(figure);
  const liveIds = new Set(
    laid.nodes.filter(n => statusClass(n) === 'open').map(n => n.id),
  );
  const nodes = laid.nodes.map(n => ({
    id: n.id,
    type: 'figure',
    position: { x: n.x, y: n.y },
    data: { ...n, statusClass: statusClass(n) },
    draggable: false,
    connectable: false,
  }));
  const edges = laid.edges.map(e => ({
    id: e.id,
    source: e.from,
    target: e.to,
    type: 'smoothstep',
    className: `fig-edge fig-edge--${e.type}`,
    animated: liveIds.has(e.from) || liveIds.has(e.to),
    markerEnd: { type: MarkerType.ArrowClosed, width: 13, height: 13 },
  }));
  return { nodes, edges };
}

function FigurePanel({ projectId, node, onClose }) {
  const px = useProjectHref();
  const ref = node.ref || {};
  const meta = node.meta || {};

  return (
    <DetailPanelShell typeLabel={node.type} title={node.label} onClose={onClose}>
      {node.status && node.status !== 'none' && (
        <div style={{ margin: '6px 0' }}><StatusPill value={String(node.status)} /></div>
      )}

      {ref.kind === 'artifact' && ref.id && (
        <>
          {meta.path && <div className="fig-panel-meta">{meta.path}</div>}
          {/* Native rendering (markdown / json / code / pdf / binary) through
              the same dispatcher the Artifacts page uses — not a raw text
              slice. ArtifactContentView owns the fetch, loading/error states,
              and per-type renderer selection. dedupeTitle keeps the panel
              header to just name + path: no leading H1 echoing the title
              already shown above. */}
          <div className="fig-panel-render">
            <ArtifactContentView
              projectId={projectId}
              artifactId={ref.id}
              size={meta.size_bytes}
              path={meta.path}
              dedupeTitle={node.label}
            />
          </div>
          <Link className="btn btn--sm" to={px(`/artifacts/${ref.id}`)}>Open in artifacts →</Link>
        </>
      )}

      {ref.kind === 'artifact_group' && (
        <>
          <div className="fig-panel-meta">
            {meta.count} additional files ({(meta.roles || []).join(', ')}) submitted for this attempt.
          </div>
          <Link className="btn btn--sm" to={px('/artifacts')}>Open artifacts →</Link>
        </>
      )}

      {ref.kind === 'claim' && (
        <>
          <div className="fig-panel-meta">{node.label}</div>
          <Link className="btn btn--sm" to={px(`/claims/${ref.id}`)}>Open claim →</Link>
        </>
      )}

      {ref.kind === 'review' && meta.notes && (
        <div className="fig-panel-notes">{meta.notes}</div>
      )}

      {ref.kind === 'sandbox' && (
        <>
          {meta.sandbox_status && <div className="fig-panel-meta">sandbox: {meta.sandbox_status}</div>}
          <a className="btn btn--sm" href="#execution">Jump to terminal →</a>
        </>
      )}

      {node.type === 'conclusion' && node.sublabel && (
        <div className="fig-panel-notes">{node.sublabel}</div>
      )}
    </DetailPanelShell>
  );
}

/**
 * ExperimentFigure — the derived figure canvas (Phase 0).
 *
 * Renders the graph served by GET /experiments/{id}/figure: the attempt
 * spine, inputs, review verdicts (with revision loops), sandbox liveness,
 * conclusion, and tested claims. Everything shown is derived server-side.
 * The agent-authored logic graph is a sibling component (LogicGraph) that
 * shares this canvas slot via ExperimentGraphs: `active` decides whether
 * this view renders, `headerExtra` carries the shared view switch, and
 * `onAvailability` tells the parent whether there is anything to show.
 */
export default function ExperimentFigure({
  projectId, experimentId, experimentStatus, attemptIndex,
  active = true, titleTabs = null, onAvailability = null,
  expanded = false, onToggleExpand = null,
}) {
  // Stored as a JSON string and only swapped when the content actually
  // changes: react-flow keys its node measurements to object identity, so
  // recreating identical node objects on every poll tick would wipe the
  // measured handle bounds and silently drop every edge.
  const [figureJson, setFigureJson] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const rfRef = useRef(null);
  const { width: panelWidth, startResize } = usePanelWidth();

  const fetchFigure = useCallback(async () => {
    try {
      const data = await api.getExperimentFigure(projectId, experimentId);
      const json = JSON.stringify(data);
      setFigureJson(prev => (prev === json ? prev : json));
    } catch {
      // Non-fatal: the rest of the page still works without the figure.
      setFigureJson(null);
    }
  }, [projectId, experimentId]);

  // Terminal experiments fetch once; live ones poll 3s only while the event
  // stream is down, otherwise refetching rides this experiment's events.
  useStreamAwarePoll(fetchFigure, {
    enabled: !TERMINAL_STATUSES.includes(experimentStatus),
    refetchKey: `${experimentStatus}:${attemptIndex}`,
    matches: (row) => row.target_id === experimentId || row.payload?.experiment_id === experimentId,
  });

  const figure = useMemo(() => (figureJson ? JSON.parse(figureJson) : null), [figureJson]);
  const { nodes, edges } = useMemo(() => toFlow(figure), [figure]);

  // Re-fit when the topology grows (new nodes), not on every poll tick.
  // Plain timer + no animation duration: animated fits ride rAF, which is
  // throttled to never in background tabs — see MeasureSync. 350ms lands
  // after MeasureSync's second measure pass.
  const topologyKey = useMemo(() => nodes.map(n => n.id).sort().join('|'), [nodes]);
  // Expanded mode may zoom past 1x so the graph actually uses the space.
  const fitMaxZoom = expanded ? 1.6 : 1;
  useEffect(() => {
    const t = setTimeout(() => rfRef.current?.fitView({ padding: 0.18, maxZoom: fitMaxZoom }), 350);
    return () => clearTimeout(t);
  }, [topologyKey, fitMaxZoom]);

  const selected = useMemo(
    () => (figure?.nodes || []).find(n => n.id === selectedId) || null,
    [figure, selectedId],
  );

  const available = Boolean(figure && (figure.nodes || []).length >= 2);
  useEffect(() => { onAvailability?.(available); }, [available, onAvailability]);

  // Refit after the canvas resizes between inline and expanded modes.
  useEffect(() => {
    const maxZoom = expanded ? 1.6 : 1;
    const t = setTimeout(() => rfRef.current?.fitView({ padding: 0.18, maxZoom }), 120);
    return () => clearTimeout(t);
  }, [expanded]);

  if (!available || !active) return null;

  return (
    <section className={`exp-figure${expanded ? ' exp-figure--expanded' : ''}`} id="figure">
      <div className="fig-head">
        <div className="fig-title">
          {titleTabs || 'Figure'}
          <span className="fig-title-hint">derived from experiment state</span>
        </div>
        <div className="fig-head-right">
          <div className="fig-legend">
            <span className="fig-chip fig-st--done">done</span>
            <span className="fig-chip fig-st--open">in motion</span>
            <span className="fig-chip fig-st--revise">needs changes</span>
            <span className="fig-chip fig-st--failed">failed</span>
            <span className="fig-chip fig-st--faded">superseded</span>
          </div>
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
      <div
        className={`fig-body${selected ? ' fig-body--split' : ''}`}
        style={{ '--fig-panel-w': `${panelWidth}px` }}
      >
        {/* Inline, the page owns the wheel: plain scrolling over the canvas
            scrolls the page (preventScrolling=false, zoomOnScroll=false) and
            zooming is reserved for unambiguous gestures — pinch / ctrl+wheel,
            the +/- controls. Expanded, page scroll is locked, so the wheel
            zooms the canvas instead. */}
        <div className="fig-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onInit={inst => { rfRef.current = inst; }}
            onNodeClick={(event, node) => {
              // Non-draggable nodes get no d3-drag click suppression, so the
              // click would bubble to the pane and immediately deselect.
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
          <FigurePanel
            projectId={projectId}
            node={selected}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </section>
  );
}
