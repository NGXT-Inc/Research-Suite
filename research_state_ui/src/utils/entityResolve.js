/**
 * The single place that turns a research-entity id (`exp_…`, `claim_…`,
 * `res_…`, `rev_…`, `rver_…`, `syn_…`) into a display label, a route, and the
 * key facts a hover card shows. Every id chip in the product resolves through
 * here so labels/routes never drift per surface (it subsumes the old
 * PostCard.refTarget, EventTimeline.targetHref, and LogicGraph.NodeRef logic).
 *
 * resolveEntity() is synchronous and reads only the home snapshot — it never
 * fetches, so a report with dozens of ids costs zero requests until a hover.
 * fetchEntity() is the lazy fallback for the rare id outside the snapshot,
 * called on hover-intent only and memoised per project.
 */
import { api } from '../api';
import { expName } from './experiment';

// The prefix is the token before the first underscore, so `rev` (a review) and
// `rver` (a specific review version) stay distinct — a plain startsWith would
// conflate them.
const PREFIX_TYPE = {
  exp: 'experiment',
  claim: 'claim',
  res: 'resource',
  rev: 'review',
  // rver_ is a resource *version* id (it appears as association.version_id), not
  // a review version — it resolves to the resource that owns the version.
  rver: 'resource_version',
  syn: 'synthesis',
};

// One geometric glyph per type (monochrome, no emoji) — claim is the hollow
// hypothesis, experiment the filled test of it.
export const TYPE_GLYPH = {
  experiment: '◆',
  claim: '◇',
  resource: '▤',
  resource_version: '▤',
  review: '◈',
  synthesis: '❖',
};

export const TYPE_LABEL = {
  experiment: 'experiment',
  claim: 'claim',
  resource: 'resource',
  resource_version: 'resource version',
  review: 'review',
  synthesis: 'reflection',
};

// Only these types have a project-scoped detail page; the rest render as a
// non-navigating chip that still gets a hover card.
const ROUTE = {
  experiment: (id) => `/experiments/${id}`,
  claim: (id) => `/claims/${id}`,
  resource: (id) => `/resources/${id}`,
};

// Matches a bare entity id in prose. `\b` at the head keeps `myexp_1` from
// matching; the trailing negative lookahead lets ids carry hyphens without the
// word boundary cutting them short. `rver` precedes `rev` so the longer prefix
// wins.
export const ENTITY_ID_RE = /\b(exp|claim|rver|rev|res|syn)_[A-Za-z0-9][\w-]*(?![\w-])/g;
// Anchored form: is a whole (trimmed) string exactly one id? Used to decide
// whether a bare id inside inline `code` should still chip.
export const ENTITY_ID_EXACT = /^(exp|claim|rver|rev|res|syn)_[A-Za-z0-9][\w-]*$/;

export function entityPrefix(id) {
  if (typeof id !== 'string') return null;
  const i = id.indexOf('_');
  return i > 0 ? id.slice(0, i) : null;
}

export function entityType(id) {
  return PREFIX_TYPE[entityPrefix(id)] || null;
}

function shortId(id) {
  return typeof id === 'string' && id.length > 14 ? `${id.slice(0, 4)}…${id.slice(-6)}` : id;
}

function basename(p) {
  return (p || '').split('/').filter(Boolean).pop() || p || '';
}

function clamp(s, n) {
  const t = (s || '').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

// --- home-snapshot field extractors (tolerant: a missing field just drops its
// line from the card, never throws) --------------------------------------

function reviewList(home) {
  const r = home?.reviews;
  if (Array.isArray(r)) return r;
  if (r && typeof r === 'object') return r.reviews || [];
  return [];
}

function reviewRequests(home) {
  const r = home?.reviews;
  return r && !Array.isArray(r) && typeof r === 'object' ? r.requests || [] : [];
}

function tms(x) {
  const t = Date.parse(x?.submitted_at || x?.created_at || x?.updated_at || '');
  return Number.isFinite(t) ? t : 0;
}

function latestReviewVerdict(home, targetId) {
  const rs = reviewList(home).filter((x) => x.target_id === targetId && x.verdict);
  if (!rs.length) return null;
  return rs.slice().sort((a, b) => tms(a) - tms(b)).pop().verdict;
}

// Claims don't carry their tests in the snapshot, but experiments carry
// `tested_claims` — count how many name this claim (null when no experiments
// are loaded, so the card omits the line rather than showing a wrong 0).
function countClaimTests(claimId, home) {
  const exps = home.experiments || [];
  if (!exps.length) return null;
  const names = (e) => e.tested_claims || e.tested_claim_ids || e.claim_ids || [];
  return exps.filter((e) => names(e).some((x) => (typeof x === 'string' ? x : x?.id || x?.claim_id) === claimId)).length;
}

function resourceRole(r) {
  return r.role || r.associations?.[0]?.role || r.kind || null;
}

function versionCount(r) {
  if (Array.isArray(r.versions)) return r.versions.length;
  if (typeof r.version_count === 'number') return r.version_count;
  return null;
}

function headlineMetric(e) {
  const m = e.headline_metric || e.primary_metric || e.metric;
  if (!m) return null;
  if (typeof m === 'object') return m.name && m.value != null ? `${m.name} ${m.value}` : null;
  return String(m);
}

function reviewLabel(rv) {
  const role = (rv.role || 'review').replace(/_reviewer$/, '');
  return rv.verdict ? `${role} · ${rv.verdict}` : role;
}

const DEAD = (id, type) => ({
  id, type, label: shortId(id), route: null, navigable: false, detail: null,
});

/**
 * Resolve an id against the home snapshot. Returns a stable shape:
 *   { id, type, label, route, navigable, detail, needsFetch?, notFound?, unresolved? }
 * `needsFetch` marks a known type that isn't in the snapshot (the chip may then
 * lazy-fetch on hover); `unresolved` marks an unrecognised prefix.
 */
export function resolveEntity(id, home) {
  const type = entityType(id);
  if (!type) return { ...DEAD(id, null), unresolved: true, notFound: true };
  const H = home || {};

  if (type === 'experiment') {
    const e = (H.experiments || []).find((x) => x.id === id);
    if (!e) return { ...DEAD(id, type), needsFetch: true };
    return {
      id, type, label: expName(e), route: ROUTE.experiment(id), navigable: true,
      detail: {
        type, name: expName(e), intent: e.intent || '', status: e.status,
        updated_at: e.updated_at, review: latestReviewVerdict(H, id), metric: headlineMetric(e),
      },
    };
  }

  if (type === 'claim') {
    const c = (H.claims || []).find((x) => x.id === id);
    if (!c) return { ...DEAD(id, type), needsFetch: true };
    return {
      id, type, label: clamp(c.statement, 44) || 'claim', route: ROUTE.claim(id), navigable: true,
      detail: {
        type, statement: c.statement || '', status: c.status,
        confidence: c.confidence, linked: countClaimTests(id, H),
      },
    };
  }

  if (type === 'resource') {
    const r = (H.resources || []).find((x) => x.id === id);
    if (!r) return { ...DEAD(id, type), needsFetch: true };
    return {
      id, type, label: basename(r.path) || r.title || 'resource', route: ROUTE.resource(id), navigable: true,
      detail: {
        type, path: r.path || '', role: resourceRole(r),
        versions: versionCount(r), updated_at: r.updated_at,
      },
    };
  }

  // A resource-version id resolves to the resource that owns the version.
  if (type === 'resource_version') {
    const r = (H.resources || []).find(
      (x) => x.current_version_id === id || (x.associations || []).some((a) => a.version_id === id),
    );
    if (!r) return { ...DEAD(id, type), notFound: true };
    return {
      id, type, label: basename(r.path) || r.title || 'resource', route: ROUTE.resource(r.id), navigable: true,
      detail: { type: 'resource', path: r.path || '', role: resourceRole(r), versions: versionCount(r), updated_at: r.updated_at },
    };
  }

  if (type === 'review') {
    const rv = reviewList(H).find((x) => x.id === id) || reviewRequests(H).find((x) => x.id === id);
    if (!rv) return { ...DEAD(id, type), needsFetch: true };
    return {
      id, type, label: reviewLabel(rv), navigable: false,
      detail: { type, role: rv.role, verdict: rv.verdict, submitted_at: rv.submitted_at || rv.created_at },
    };
  }

  // synthesis: never carried in the home snapshot — resolve by fetch.
  return { ...DEAD(id, type), label: 'reflection', needsFetch: true };
}

/**
 * Build a resolved entity from a LogicGraph `ref_index` entry — the server
 * already resolved these refs, so the chip should trust that instead of
 * re-deriving from the snapshot (or fetching). Returns a `seed` for EntityChip.
 */
export function seedFromRefIndex(refString, entry) {
  if (!entry || !entry.type) return null;
  const t = entry.type;
  if (t === 'resource') {
    return {
      id: refString, type: 'resource', label: entry.title || basename(entry.path) || entry.kind || 'resource',
      route: entry.resource_id ? ROUTE.resource(entry.resource_id) : null, navigable: !!entry.resource_id,
      detail: { type: 'resource', path: entry.path || '', role: entry.role || entry.kind },
    };
  }
  if (t === 'claim') {
    return {
      id: refString, type: 'claim', label: clamp(entry.statement, 44) || 'claim',
      route: entry.claim_id ? ROUTE.claim(entry.claim_id) : null, navigable: !!entry.claim_id,
      detail: { type: 'claim', statement: entry.statement || '', status: entry.status, confidence: entry.confidence },
    };
  }
  if (t === 'experiment') {
    return {
      id: refString, type: 'experiment', label: entry.name || clamp(entry.intent, 40) || 'experiment',
      route: entry.experiment_id ? ROUTE.experiment(entry.experiment_id) : null, navigable: !!entry.experiment_id,
      detail: { type: 'experiment', name: entry.name, intent: entry.intent || '', status: entry.status },
    };
  }
  if (t === 'review') {
    return {
      id: refString, type: 'review', label: reviewLabel(entry), navigable: false,
      detail: { type: 'review', role: entry.role, verdict: entry.verdict },
    };
  }
  if (t === 'synthesis') {
    return {
      id: refString, type: 'synthesis', label: entry.title || 'reflection', navigable: false,
      detail: { type: 'synthesis', status: entry.status, decision: entry.decision },
    };
  }
  return null;
}

// --- lazy fallback fetch (hover-intent only), memoised per project --------

let cachePid = null;
const cache = new Map();

function ensureProject(pid) {
  if (cachePid !== pid) { cachePid = pid; cache.clear(); }
}

/**
 * Fetch the one entity behind an id that wasn't in the snapshot. Only the
 * types with a clean single-object endpoint are fetched; the rest resolve to a
 * "not found in this project" card. Never called on render — only on hover.
 */
export async function fetchEntity(id, pid) {
  ensureProject(pid);
  if (cache.has(id)) return cache.get(id);
  const type = entityType(id);
  let out = { id, type, label: shortId(id), route: null, navigable: false, detail: null, notFound: true };
  try {
    if (type === 'experiment') {
      const s = await api.getExperimentStatus(pid, id);
      const e = s?.experiment || s || {};
      const name = (e.name || '').trim();
      out = {
        id, type, label: name || shortId(id), route: ROUTE.experiment(id), navigable: true,
        detail: { type, name, intent: e.intent || '', status: e.status || e.state, updated_at: e.updated_at, metric: headlineMetric(e) },
      };
    } else if (type === 'claim') {
      const s = await api.getClaim(pid, id);
      const c = s?.claim || s || {};
      out = {
        id, type, label: clamp(c.statement, 44) || shortId(id), route: ROUTE.claim(id), navigable: true,
        detail: { type, statement: c.statement || '', status: c.status, confidence: c.confidence, linked: null },
      };
    } else if (type === 'synthesis') {
      const s = await api.getSynthesis(pid, id);
      const w = s?.synthesis || s || {};
      out = {
        id, type, label: 'reflection', navigable: false,
        detail: { type, status: w.status, decision: w.decision },
      };
    }
  } catch {
    // leave the notFound default — the card shows the raw id + a quiet note.
  }
  // The project may have switched while the fetch was in flight; caching then
  // would poison the new project's map with the old project's entity.
  if (cachePid === pid) cache.set(id, out);
  return out;
}

// Drop the memo when the active project changes (call from a project-switch
// effect); resolveEntity results are snapshot-derived and need no eviction.
export function invalidateEntityCache() {
  cachePid = null;
  cache.clear();
}
