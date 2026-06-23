/**
 * LogicGraphHero — the first-run backdrop. A ghostly, self-forming version of
 * the product's real logic graph (Claim → Approach → Attempts → Outcome), drawn
 * in the same verdict palette as <VisualDag>. The empty state thus previews what
 * research_plugin actually builds, instead of showing a bare form in a void.
 *
 * Pure SVG + CSS keyframes (no rAF loop), deterministic layout, decorative only
 * (aria-hidden, pointer-events: none). All motion is gated behind
 * prefers-reduced-motion in global.css.
 */

// Four loose columns, mirroring the DAG's layers. Tones map to CSS palette vars.
const NODES = [
  { id: 'c1', x: 210, y: 250, r: 9, tone: 'active' },
  { id: 'c2', x: 180, y: 430, r: 8, tone: 'active' },
  { id: 'c3', x: 150, y: 610, r: 7, tone: 'active' },
  { id: 'a1', x: 500, y: 165, r: 6, tone: 'ice' },
  { id: 'a2', x: 480, y: 330, r: 7, tone: 'ice' },
  { id: 'a3', x: 520, y: 495, r: 6, tone: 'ice' },
  { id: 'a4', x: 470, y: 640, r: 5, tone: 'faint' },
  { id: 't1', x: 790, y: 130, r: 5, tone: 'faint' },
  { id: 't2', x: 800, y: 255, r: 6, tone: 'qualifies' },
  { id: 't3', x: 770, y: 370, r: 5, tone: 'faint' },
  { id: 't4', x: 810, y: 480, r: 6, tone: 'faint' },
  { id: 't5', x: 780, y: 600, r: 5, tone: 'qualifies' },
  { id: 'o1', x: 1040, y: 195, r: 8, tone: 'supports', halo: true },
  { id: 'o2', x: 1060, y: 330, r: 7, tone: 'refutes', halo: true },
  { id: 'o3', x: 1030, y: 460, r: 8, tone: 'supports', halo: true },
  { id: 'o4', x: 1050, y: 595, r: 6, tone: 'qualifies', halo: true },
];

const EDGES = [
  ['c1', 'a1'], ['c1', 'a2'], ['c2', 'a2'], ['c2', 'a3'], ['c3', 'a3'], ['c3', 'a4'],
  ['a1', 't1'], ['a1', 't2'], ['a2', 't2'], ['a2', 't3'], ['a3', 't4'], ['a3', 't5'], ['a4', 't5'],
  ['t1', 'o1'], ['t2', 'o1'], ['t2', 'o2'], ['t3', 'o3'], ['t4', 'o3'], ['t4', 'o4'], ['t5', 'o4'],
];

const TONE_VAR = {
  active: 'var(--active)',
  ice: 'var(--ice)',
  qualifies: 'var(--qualifies)',
  supports: 'var(--supports)',
  refutes: 'var(--refutes)',
  faint: 'var(--faint)',
};

const byId = Object.fromEntries(NODES.map(n => [n.id, n]));

export default function LogicGraphHero() {
  return (
    <svg
      className="boot-graph"
      viewBox="0 0 1200 720"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      <g className="boot-graph__edges">
        {EDGES.map(([s, t], i) => {
          const a = byId[s], b = byId[t];
          const d = `M${a.x} ${a.y} L${b.x} ${b.y}`;
          // Travelling pulse staggered left→right so the graph reads as "filling in".
          const delay = `${(a.x / 1200) * 2.4 + (i % 3) * 0.3}s`;
          return (
            <g key={i}>
              <path className="boot-edge" d={d} pathLength="1" />
              <path className="boot-edge boot-edge--flow" d={d} pathLength="1" style={{ animationDelay: delay }} />
            </g>
          );
        })}
      </g>
      <g className="boot-graph__nodes">
        {NODES.map((n, i) => (
          <g key={n.id} className="boot-node">
            {n.halo && (
              <circle className="boot-node__halo" cx={n.x} cy={n.y} r={n.r} style={{ color: TONE_VAR[n.tone], animationDelay: `${i * 0.5}s` }} />
            )}
            <circle
              className="boot-node__dot"
              cx={n.x}
              cy={n.y}
              r={n.r}
              style={{ fill: TONE_VAR[n.tone], animationDelay: `${i * 0.4}s` }}
            />
          </g>
        ))}
      </g>
    </svg>
  );
}
