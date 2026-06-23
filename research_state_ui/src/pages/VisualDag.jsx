import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useProjectStore,
  useProjectHref,
  selectClaims,
  selectExperiments,
  selectEventsAll,
} from '../store/useProjectStore';
import {
  outcomeColor,
  outcomeGlyph,
  outcomeLabel,
} from '../utils/evidence';
import {
  attemptTimings,
  buildLogicDag,
  layoutLayeredDag,
  projectTimeScale,
  summarizeClaim,
} from '../utils/graph';
import { expName } from '../utils/experiment';

const STATUS_FILL = {
  active:       'var(--active)',
  supported:    'var(--supports)',
  weakened:     'var(--qualifies)',
  contradicted: 'var(--refutes)',
  draft:        'var(--faint)',
  abandoned:    'var(--line-strong)',
};

const LAYER_LABELS = ['Claim', 'Approach', 'Attempts', 'Outcome'];

// Widened viewBox so each experiment (now positioned by global time, not
// constrained by a per-claim lane) has room for its title + date label.
const VIEW_W = 1600;
const VIEW_H = 820;

export default function VisualDag() {
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEventsAll);
  const [hoverId, setHoverId] = useState(null);
  const [focusClaimId, setFocusClaimId] = useState(null);
  const [highlightWinning, setHighlightWinning] = useState(true);

  const projectScale = useMemo(
    () => projectTimeScale(claims, experiments, events),
    [claims, experiments, events],
  );

  const dag = useMemo(() => {
    const built = buildLogicDag(claims, experiments);
    return layoutLayeredDag(built, VIEW_W, VIEW_H, focusClaimId, projectScale);
  }, [claims, experiments, focusClaimId, projectScale]);

  // Per-experiment attempt timings: experiment.id → { starts: [ms,...], endOrNow }
  const expTimings = useMemo(() => {
    const map = new Map();
    for (const e of experiments) {
      map.set(e.id, attemptTimings(e, events));
    }
    return map;
  }, [experiments, events]);

  const byId = useMemo(
    () => new Map(dag.nodes.map(n => [n.id, n])),
    [dag],
  );

  const visibleNodes = useMemo(
    () => dag.nodes.filter(n => !n.hidden),
    [dag],
  );
  const visibleEdges = useMemo(
    () => dag.edges.filter(e => !byId.get(e.source)?.hidden && !byId.get(e.target)?.hidden),
    [dag.edges, byId],
  );

  // Lineage walk = full connected component from the hovered node, so the
  // viewer sees the entire chain that flows through it.
  const lineage = useMemo(() => {
    if (!hoverId) return null;
    const set = new Set();
    const adj = new Map();
    for (const e of visibleEdges) {
      if (!adj.has(e.source)) adj.set(e.source, []);
      if (!adj.has(e.target)) adj.set(e.target, []);
      adj.get(e.source).push(e.target);
      adj.get(e.target).push(e.source);
    }
    const stack = [hoverId];
    while (stack.length) {
      const id = stack.pop();
      if (set.has(id)) continue;
      set.add(id);
      for (const next of adj.get(id) || []) stack.push(next);
    }
    return set;
  }, [hoverId, visibleEdges]);

  const winning = highlightWinning ? dag.winningPath : null;

  const claimSummaries = useMemo(() => {
    const map = new Map();
    for (const c of dag.layers[0]) {
      map.set(c.ref.id, summarizeClaim(c, dag));
    }
    return map;
  }, [dag]);

  const visibleClaims = dag.layers[0].filter(c => !c.hidden);
  const totalApproaches = dag.layers[1].filter(n => !n.hidden).length;
  const totalAttempts = dag.layers[2].filter(n => !n.hidden).length;

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <h1 className="page-title">What we tried, in what order, with what result</h1>
        <p className="page-summary">Every claim, its attempts, and where each landed.</p>
      </header>

      <section className="section">
        <div className="vd-toolbar">
          <div className="vd-toolbar-stats">
            <span><strong>{visibleClaims.length}</strong> claim{visibleClaims.length === 1 ? '' : 's'}</span>
            <span className="vd-toolbar-dot">·</span>
            <span><strong>{totalApproaches}</strong> approach{totalApproaches === 1 ? '' : 'es'}</span>
            <span className="vd-toolbar-dot">·</span>
            <span><strong>{totalAttempts}</strong> attempt{totalAttempts === 1 ? '' : 's'}</span>
            {projectScale && (
              <>
                <span className="vd-toolbar-dot">·</span>
                <span title={`${formatDate(projectScale.start)} → ${formatDate(projectScale.end)}`}>
                  spans <strong>{daysBetween(projectScale.start, projectScale.end)}</strong> day{daysBetween(projectScale.start, projectScale.end) === 1 ? '' : 's'}
                  {' '}({formatDate(projectScale.start)} → {formatDate(projectScale.end)})
                </span>
              </>
            )}
            {focusClaimId && (
              <button
                type="button"
                className="btn btn--sm btn--ghost vd-toolbar-clear"
                onClick={() => setFocusClaimId(null)}
              >
                ← Show all claims
              </button>
            )}
          </div>
          <div className="vd-toolbar-right">
            <label className="vd-toggle">
              <input
                type="checkbox"
                checked={highlightWinning}
                onChange={e => setHighlightWinning(e.target.checked)}
              />
              Highlight winning paths
            </label>
            <Legend />
          </div>
        </div>

        {dag.nodes.length === 0 ? (
          <div className="empty-state">
            <h2>Nothing to chart yet</h2>
          </div>
        ) : (
          <div className="vd-canvas-wrap">
            <div className="vd-layer-rail">
              {LAYER_LABELS.map(label => (
                <div key={label} className="vd-layer-tick">{label}</div>
              ))}
            </div>
            <svg
              viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
              preserveAspectRatio="xMidYMid meet"
              className="vd-canvas"
              onMouseLeave={() => setHoverId(null)}
            >
              {/* Global time axis — the single horizontal organizer. Day
                  ticks at top + bottom anchor every node to a date. Subtle
                  vertical gridlines through the body let viewers eyeball
                  "when did this happen" without measuring. */}
              {dag.timeAxis && (
                <TimeAxis axis={dag.timeAxis} />
              )}

              {/* Edges (drawn beneath nodes) */}
              {visibleEdges.map((e, i) => {
                const a = byId.get(e.source);
                const b = byId.get(e.target);
                if (!a || !b) return null;
                const onWinning = winning && winning.has(a.id) && winning.has(b.id);
                const onLineage = lineage && lineage.has(a.id) && lineage.has(b.id);
                const dim = lineage && !onLineage;
                let stroke = 'var(--line-strong)';
                let dasharray = null;
                let strokeWidth = 1.2;
                if (e.type === 'attempts') {
                  stroke = 'var(--muted)';
                  dasharray = '3 4';
                  strokeWidth = 0.9;
                } else if (e.outcome) {
                  stroke = outcomeColor(e.outcome);
                  strokeWidth = onWinning ? 3.0 : 1.6;
                }
                const opacity = dim ? 0.06 : (onWinning ? 0.95 : 0.5);
                const mid = (a.y + b.y) / 2;
                const path = `M ${a.x} ${a.y} C ${a.x} ${mid}, ${b.x} ${mid}, ${b.x} ${b.y}`;
                return (
                  <path
                    key={i}
                    d={path}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={strokeWidth}
                    strokeOpacity={opacity}
                    strokeDasharray={dasharray}
                  />
                );
              })}

              {/* Nodes (drawn above edges) */}
              {visibleNodes.map(n => {
                const onWinning = winning?.has(n.id);
                const onLineage = lineage?.has(n.id);
                const dim = lineage && !onLineage;
                return (
                  <DagNode
                    key={n.id}
                    node={n}
                    dim={dim}
                    isHover={n.id === hoverId}
                    onWinning={!!onWinning}
                    onHover={setHoverId}
                    onClaimClick={(claimId) =>
                      setFocusClaimId(prev => (prev === claimId ? null : claimId))
                    }
                    summary={n.kind === 'claim' ? claimSummaries.get(n.ref.id) : null}
                    timings={n.kind === 'experiment' || n.kind === 'attempt'
                      ? expTimings.get(n.ref.id)
                      : null}
                  />
                );
              })}
            </svg>

            {hoverId && byId.get(hoverId) && (
              <HoverInfo
                node={byId.get(hoverId)}
                summary={byId.get(hoverId).kind === 'claim'
                  ? claimSummaries.get(byId.get(hoverId).ref.id)
                  : null}
              />
            )}
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-title">How to read</div>
        <ul className="vd-guide">
          <li>
            <strong>Claims</strong> sit at the top. Click one to filter the
            chart to its sub-tree — useful when there are many claims.
          </li>
          <li>
            <strong>Approaches</strong> are the experiments tested against
            each claim. They're laid out oldest-to-newest left-to-right inside
            their claim's lane.
          </li>
          <li>
            <strong>Attempt chains</strong> hang straight down from each
            approach, top-to-bottom in revision order. The final attempt
            shows the outcome glyph (✓ ✗ ? ◐ ·). Dashed connectors are
            sibling-revision links.
          </li>
          <li>
            <strong>Outcome buckets</strong> at the bottom show how many
            approaches landed there and how many total attempts of effort
            funneled in.
          </li>
        </ul>
      </section>
    </div>
  );
}

/**
 * Wrap a label into N lines without breaking on character count alone.
 * Returns an array of strings ready to be rendered as <tspan>s.
 */
function wrapLabel(label, maxChars, maxLines = 2) {
  if (!label) return [];
  const words = String(label).split(/\s+/);
  const lines = [];
  let current = '';
  for (const w of words) {
    if ((current + ' ' + w).trim().length <= maxChars) {
      current = (current + ' ' + w).trim();
    } else {
      if (current) lines.push(current);
      current = w;
      if (lines.length >= maxLines - 1) break;
    }
  }
  if (current && lines.length < maxLines) lines.push(current);
  // If we ran out of room, tail-truncate the final line.
  if (lines.length === maxLines) {
    const flatLen = lines.join(' ').length;
    if (flatLen < String(label).length) {
      lines[maxLines - 1] = (lines[maxLines - 1].slice(0, maxChars - 1).trimEnd()) + '…';
    }
  }
  return lines;
}

function DagNode({ node, dim, isHover, onWinning, onHover, onClaimClick, summary, timings }) {
  const opacity = dim ? 0.18 : 1;
  const onMouse = () => onHover(node.id);

  if (node.kind === 'claim') {
    const w = 230;
    const h = 78;
    const lines = wrapLabel(node.label, 36, 3);
    const statusColor = STATUS_FILL[node.status] || 'var(--active)';
    const createdLabel = node.ref.created_at ? formatDate(Date.parse(node.ref.created_at)) : null;
    return (
      <g
        style={{ cursor: 'pointer' }}
        onMouseEnter={onMouse}
        onMouseOver={onMouse}
        onClick={() => onClaimClick(node.ref.id)}
        opacity={opacity}
      >
        <rect
          x={node.x - w / 2} y={node.y - 8}
          width={w} height={h}
          rx={8} ry={8}
          fill="var(--bg-elev)"
          stroke={statusColor}
          strokeWidth={onWinning || isHover ? 2.5 : 1.8}
        />
        <text
          x={node.x} y={node.y + 10}
          textAnchor="middle" fontSize="11.5"
          fill="var(--text)"
        >
          {lines.map((line, i) => (
            <tspan key={i} x={node.x} dy={i === 0 ? 0 : 14}>{line}</tspan>
          ))}
        </text>
        {summary && (
          <g>
            <ClaimChips x={node.x} y={node.y + h - 18} summary={summary} />
          </g>
        )}
        {createdLabel && (
          <text
            x={node.x} y={node.y + h + 12}
            textAnchor="middle" fontSize="9.5"
            fill="var(--muted)"
            className="tabular"
          >
            started {createdLabel}
          </text>
        )}
      </g>
    );
  }

  if (node.kind === 'experiment') {
    const w = 150;
    const h = 44;
    const title = expName(node.ref);
    const lines = wrapLabel(title, 22, 2);
    const fill = outcomeColor(node.outcome);
    const created = node.ref.created_at ? formatDate(Date.parse(node.ref.created_at)) : null;
    return (
      <g
        style={{ cursor: 'pointer' }}
        onMouseEnter={onMouse}
        onMouseOver={onMouse}
        opacity={opacity}
      >
        <rect
          x={node.x - w / 2} y={node.y - h / 2}
          width={w} height={h}
          rx={5} ry={5}
          fill={fill}
          fillOpacity={0.12}
          stroke={fill}
          strokeWidth={onWinning ? 2.4 : isHover ? 1.8 : 1.3}
        />
        <text
          x={node.x} y={node.y - 2}
          textAnchor="middle" fontSize="11"
          fill="var(--text)"
        >
          {lines.map((line, i) => (
            <tspan key={i} x={node.x} dy={i === 0 ? 0 : 12}>{line}</tspan>
          ))}
        </text>
        {created && (
          <text
            x={node.x} y={node.y + h / 2 + 11}
            textAnchor="middle" fontSize="9"
            fill="var(--muted)"
            className="tabular"
          >
            {created}
          </text>
        )}
      </g>
    );
  }

  if (node.kind === 'attempt') {
    // Compute the start date for this attempt, when timings are available.
    let attemptDate = null;
    if (timings?.starts && timings.starts[node.attempt - 1]) {
      attemptDate = formatDate(timings.starts[node.attempt - 1]);
    }
    if (node.isFinal) {
      const r = 13;
      // Show the latest activity date alongside the final outcome — that's
      // the "we landed here on …" stamp.
      const finalDate = timings?.endOrNow ? formatDate(timings.endOrNow) : null;
      return (
        <g
          style={{ cursor: 'pointer' }}
          onMouseEnter={onMouse}
          onMouseOver={onMouse}
          opacity={opacity}
        >
          <circle
            cx={node.x} cy={node.y} r={r + (isHover ? 3 : 0)}
            fill={outcomeColor(node.outcome)}
            fillOpacity={0.92}
            stroke="var(--bg-elev)"
            strokeWidth={2.5}
          />
          <text
            x={node.x} y={node.y + 4}
            textAnchor="middle" fontSize="13"
            fill="#fff" fontWeight="700"
          >
            {outcomeGlyph(node.outcome)}
          </text>
          <text
            x={node.x} y={node.y - r - 6}
            textAnchor="middle" fontSize="9.5"
            fill="var(--muted)"
          >
            v{node.attempt}
          </text>
          {finalDate && (
            <text
              x={node.x + r + 4} y={node.y + 4}
              fontSize="9"
              fill="var(--muted)"
              className="tabular"
            >
              {finalDate}
            </text>
          )}
        </g>
      );
    }
    // Intermediate attempt — visually marked as a dead-end revision.
    const r = 5;
    return (
      <g
        style={{ cursor: 'pointer' }}
        onMouseEnter={onMouse}
        onMouseOver={onMouse}
        opacity={opacity}
      >
        <circle
          cx={node.x} cy={node.y} r={r + (isHover ? 1.5 : 0)}
          fill="var(--bg-soft)"
          stroke={onWinning ? 'var(--qualifies)' : 'var(--line-strong)'}
          strokeWidth={onWinning ? 1.5 : 1}
        />
        <text
          x={node.x + 10} y={node.y + 3}
          fontSize="9"
          fill="var(--faint)"
        >
          v{node.attempt}{attemptDate ? ` · ${attemptDate}` : ''}
        </text>
      </g>
    );
  }

  if (node.kind === 'outcome') {
    const w = 130;
    const h = 36;
    const fill = outcomeColor(node.outcome);
    return (
      <g
        style={{ cursor: 'pointer' }}
        onMouseEnter={onMouse}
        onMouseOver={onMouse}
        opacity={opacity}
      >
        <rect
          x={node.x - w / 2} y={node.y - h / 2}
          width={w} height={h}
          rx={h / 2} ry={h / 2}
          fill={fill}
          fillOpacity={onWinning ? 0.55 : 0.22}
          stroke={fill}
          strokeWidth={onWinning ? 2.2 : 1.2}
        />
        <text
          x={node.x} y={node.y - 1}
          textAnchor="middle" fontSize="11.5"
          fill={fill} fontWeight="700"
        >
          {node.label}
        </text>
        <text
          x={node.x} y={node.y + 12}
          textAnchor="middle" fontSize="9.5"
          fill="var(--muted)"
        >
          {node.experimentCount} approach{node.experimentCount === 1 ? '' : 'es'} · {node.attemptCount} attempt{node.attemptCount === 1 ? '' : 's'}
        </text>
      </g>
    );
  }

  return null;
}

function ClaimChips({ x, y, summary }) {
  // Render the per-claim tally as a compact inline row of "N glyph" chips.
  const items = [];
  if (summary.tally.supports) items.push({ glyph: '✓', n: summary.tally.supports, color: 'var(--supports)' });
  if (summary.tally.refutes) items.push({ glyph: '✗', n: summary.tally.refutes, color: 'var(--refutes)' });
  if (summary.tally.qualifies) items.push({ glyph: '?', n: summary.tally.qualifies, color: 'var(--qualifies)' });
  if (summary.tally.inflight) items.push({ glyph: '◐', n: summary.tally.inflight, color: 'var(--active)' });
  if (summary.tally.abandoned) items.push({ glyph: '·', n: summary.tally.abandoned, color: 'var(--faint)' });

  if (summary.tested === 0) {
    return (
      <text x={x} y={y} textAnchor="middle" fontSize="10" fill="var(--faint)">
        no experiments yet
      </text>
    );
  }

  const itemW = 30;
  const totalW = items.length * itemW;
  const startX = x - totalW / 2;
  return (
    <g>
      {items.map((it, i) => (
        <g key={i} transform={`translate(${startX + i * itemW + itemW / 2}, ${y})`}>
          <text
            textAnchor="middle"
            fontSize="11"
            fill={it.color}
            fontWeight="700"
          >
            <tspan>{it.glyph}</tspan>
            <tspan dx="3" fill="var(--muted)" fontWeight="500">{it.n}</tspan>
          </text>
        </g>
      ))}
    </g>
  );
}

function HoverInfo({ node, summary }) {
  const px = useProjectHref();
  let title, body, sub, link;
  if (node.kind === 'claim') {
    title = 'Claim';
    body = node.ref.statement;
    sub = summary
      ? `${summary.tested} approach${summary.tested === 1 ? '' : 'es'} · ${summary.attempts} attempt${summary.attempts === 1 ? '' : 's'} of effort · status ${node.status || 'active'}`
      : `status ${node.status || 'active'}`;
    link = px(`/claims/${node.ref.id}`);
  } else if (node.kind === 'experiment') {
    title = `Approach · ${outcomeLabel(node.outcome)}`;
    body = node.ref.intent || expName(node.ref);
    sub = `status ${node.status} · ${node.attempt_index} attempt${node.attempt_index === 1 ? '' : 's'} so far`;
    link = px(`/experiments/${node.ref.id}`);
  } else if (node.kind === 'attempt') {
    title = node.isFinal
      ? `Final attempt · v${node.attempt} · ${outcomeLabel(node.outcome)}`
      : `Earlier attempt · v${node.attempt}`;
    body = expName(node.ref);
    sub = node.isFinal
      ? 'The chain landed here.'
      : 'An earlier revision — work moved on after this attempt.';
    link = px(`/experiments/${node.ref.id}`);
  } else if (node.kind === 'outcome') {
    title = `${node.label} bucket`;
    body = `${node.experimentCount} approach${node.experimentCount === 1 ? '' : 'es'} for this claim landed here.`;
    sub = `${node.attemptCount} attempt${node.attemptCount === 1 ? '' : 's'} of research effort funneled into this outcome.`;
  }
  return (
    <div className="vd-hover">
      <div className="vd-hover-title">{title}</div>
      <div className="vd-hover-body">{body}</div>
      {sub && <div className="vd-hover-sub">{sub}</div>}
      {link && (
        <Link to={link} className="btn btn--sm btn--ghost" style={{ marginTop: 8 }}>
          Open detail →
        </Link>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="vd-legend">
      <LegendItem color="var(--supports)" glyph="✓" label="passed" />
      <LegendItem color="var(--qualifies)" glyph="?" label="needs changes" />
      <LegendItem color="var(--refutes)" glyph="✗" label="failed" />
      <LegendItem color="var(--active)" glyph="◐" label="in flight" />
      <LegendItem color="var(--faint)" glyph="·" label="abandoned" />
    </div>
  );
}

function LegendItem({ color, glyph, label }) {
  return (
    <span className="vd-legend-item">
      <span
        className="vd-legend-glyph"
        style={{ color, borderColor: color }}
      >
        {glyph}
      </span>
      {label}
    </span>
  );
}

// ----- Chronology helpers ---------------------------------------------------

function formatDate(ms) {
  if (!Number.isFinite(ms)) return '';
  return new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function daysBetween(startMs, endMs) {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return 0;
  return Math.max(1, Math.round((endMs - startMs) / (24 * 60 * 60 * 1000)));
}

/**
 * Render the global time axis: top + bottom horizontal lines with day tick
 * marks, plus subtle vertical gridlines through the chart body. Tick spacing
 * adapts so a 6-day span shows daily ticks while a 60-day span shows weekly.
 */
function TimeAxis({ axis }) {
  const { tStart, tEnd, xAt, padX, padTop, padBot, innerW, height } = axis;
  const dayMs = 24 * 60 * 60 * 1000;
  const totalDays = Math.max(1, (tEnd - tStart) / dayMs);
  // Aim for ~10 ticks across the axis.
  const idealStepDays = Math.max(1, Math.round(totalDays / 10));
  // Snap to a tidy step (1, 2, 5, 7, 14, 30 days).
  const tidy = [1, 2, 5, 7, 14, 30, 60, 90];
  const stepDays = tidy.find(s => s >= idealStepDays) || tidy[tidy.length - 1];

  // Align ticks to UTC midnight at the start.
  const startDay = new Date(tStart);
  startDay.setHours(0, 0, 0, 0);
  const ticks = [];
  for (let t = startDay.getTime(); t <= tEnd; t += stepDays * dayMs) {
    if (t >= tStart - dayMs && t <= tEnd + dayMs) {
      ticks.push(t);
    }
  }

  const topY = padTop - 24;
  const botY = height - padBot + 24;

  return (
    <g className="vd-time-axis">
      {/* Top axis line + tick labels */}
      <line
        x1={padX} x2={padX + innerW}
        y1={topY} y2={topY}
        stroke="var(--line-strong)"
        strokeWidth="1"
      />
      {ticks.map((t, i) => (
        <g key={`top-${i}`}>
          <line
            x1={xAt(t)} x2={xAt(t)}
            y1={topY} y2={topY + 4}
            stroke="var(--muted)"
            strokeWidth="1"
          />
          <text
            x={xAt(t)} y={topY - 6}
            textAnchor="middle"
            fontSize="10"
            fill="var(--muted)"
            className="tabular"
          >
            {formatDate(t)}
          </text>
          {/* Subtle vertical gridline through the body */}
          <line
            x1={xAt(t)} x2={xAt(t)}
            y1={topY + 4} y2={botY - 4}
            stroke="var(--line-soft)"
            strokeWidth="1"
            strokeDasharray="2 6"
          />
        </g>
      ))}
      {/* Bottom axis line + tick labels */}
      <line
        x1={padX} x2={padX + innerW}
        y1={botY} y2={botY}
        stroke="var(--line-strong)"
        strokeWidth="1"
      />
      {ticks.map((t, i) => (
        <g key={`bot-${i}`}>
          <line
            x1={xAt(t)} x2={xAt(t)}
            y1={botY - 4} y2={botY}
            stroke="var(--muted)"
            strokeWidth="1"
          />
          <text
            x={xAt(t)} y={botY + 14}
            textAnchor="middle"
            fontSize="10"
            fill="var(--muted)"
            className="tabular"
          >
            {formatDate(t)}
          </text>
        </g>
      ))}
    </g>
  );
}
