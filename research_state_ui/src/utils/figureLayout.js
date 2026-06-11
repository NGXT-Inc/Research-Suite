/**
 * Layered left-to-right layout for the experiment figure graph.
 *
 * The figures are small DAGs (attempt spine + satellites), so a full layout
 * engine is overkill: longest-path layering gives the reading order —
 * inputs → attempt 1 → review → attempt 2 → … → conclusion → claims — and a
 * right-pack pass pulls pure sources (e.g. a plan registered at attempt 2)
 * next to their consumer instead of stranding them at column 0. Deterministic
 * by construction: same figure JSON → same positions, so polling never
 * reshuffles the canvas.
 */

export const FIG_NODE_W = 196;
const FIG_NODE_H = 66;
const GAP_X = 72;
const GAP_Y = 20;

// Vertical order within a column: inputs above the spine, verdicts/outputs below.
const TYPE_ORDER = { resource: 0, resource_group: 1, attempt: 2, sandbox: 3, review: 4, conclusion: 5, claim: 6 };

export function layoutFigure(figure) {
  const rawNodes = figure?.nodes || [];
  const rawEdges = figure?.edges || [];
  if (!rawNodes.length) return { nodes: [], edges: [] };

  const ids = new Set(rawNodes.map(n => n.id));
  const edges = rawEdges.filter(e => ids.has(e.from) && ids.has(e.to) && e.from !== e.to);

  const out = new Map(rawNodes.map(n => [n.id, []]));
  const indeg = new Map(rawNodes.map(n => [n.id, 0]));
  for (const e of edges) {
    out.get(e.from).push(e.to);
    indeg.set(e.to, indeg.get(e.to) + 1);
  }

  // Longest-path layering via Kahn's order (cycle-safe: leftovers keep rank 0).
  const rank = new Map(rawNodes.map(n => [n.id, 0]));
  const remaining = new Map(indeg);
  const queue = rawNodes.filter(n => indeg.get(n.id) === 0).map(n => n.id);
  while (queue.length) {
    const id = queue.shift();
    for (const next of out.get(id)) {
      rank.set(next, Math.max(rank.get(next), rank.get(id) + 1));
      remaining.set(next, remaining.get(next) - 1);
      if (remaining.get(next) === 0) queue.push(next);
    }
  }

  // Right-pack sources: a node with no inputs sits just left of its earliest consumer.
  for (const n of rawNodes) {
    const targets = out.get(n.id);
    if (indeg.get(n.id) === 0 && targets.length) {
      const minSucc = Math.min(...targets.map(t => rank.get(t)));
      if (Number.isFinite(minSucc)) rank.set(n.id, Math.max(rank.get(n.id), minSucc - 1));
    }
  }

  const columns = new Map();
  for (const n of rawNodes) {
    const r = rank.get(n.id) || 0;
    if (!columns.has(r)) columns.set(r, []);
    columns.get(r).push(n);
  }

  const tallest = Math.max(...[...columns.values()].map(col => col.length));
  const totalH = tallest * FIG_NODE_H + (tallest - 1) * GAP_Y;

  const nodes = [];
  for (const [r, col] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    col.sort((a, b) => {
      const ka = `${a.group || ''}~${TYPE_ORDER[a.type] ?? 9}~${a.id}`;
      const kb = `${b.group || ''}~${TYPE_ORDER[b.type] ?? 9}~${b.id}`;
      return ka.localeCompare(kb);
    });
    const colH = col.length * FIG_NODE_H + (col.length - 1) * GAP_Y;
    let y = (totalH - colH) / 2;
    for (const n of col) {
      nodes.push({ ...n, x: r * (FIG_NODE_W + GAP_X), y });
      y += FIG_NODE_H + GAP_Y;
    }
  }
  return { nodes, edges };
}
