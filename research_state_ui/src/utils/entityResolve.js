/**
 * The single place that turns a research-entity id (`exp_…`, `claim_…`,
 * `art_…`, `rev_…`, `syn_…`) into a display label, a route, and the
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

// The prefix is the token before the first underscore.
const PREFIX_TYPE = {
  exp: 'experiment',
  claim: 'claim',
  art: 'artifact',
  rev: 'review',
  syn: 'reflection',
  lit: 'litreview_section',
  paper: 'paper',
};

// One geometric glyph per type (monochrome, no emoji) — claim is the hollow
// hypothesis, experiment the filled test of it.
export const TYPE_GLYPH = {
  experiment: '◆',
  claim: '◇',
  artifact: '▤',
  review: '◈',
  reflection: '❖',
  litreview_section: '▤',
  paper: '▧',
};

export const TYPE_LABEL = {
  experiment: 'experiment',
  claim: 'claim',
  artifact: 'artifact',
  review: 'review',
  reflection: 'reflection',
  litreview_section: 'lit review section',
  paper: 'paper',
};

// Only these types have a project-scoped detail page; the rest render as a
// non-navigating chip that still gets a hover card.
const ROUTE = {
  experiment: (id) => `/experiments/${id}`,
  claim: (id) => `/claims/${id}`,
  artifact: (id) => `/artifacts/${id}`,
  // Sections and papers live on the one lit-review screen (no per-id page).
  litreview_section: () => '/litreview',
  paper: () => '/litreview',
};

// Matches a bare entity id in prose. `\b` at the head keeps `myexp_1` from
// matching; the trailing negative lookahead lets ids carry hyphens without the
// word boundary cutting them short.
export const ENTITY_ID_RE = /\b(exp|claim|rev|art|syn|lit|paper)_[A-Za-z0-9][\w-]*(?![\w-])/g;
// Anchored form: is a whole (trimmed) string exactly one id? Used to decide
// whether a bare id inside inline `code` should still chip.
export const ENTITY_ID_EXACT = /^(exp|claim|rev|art|syn|lit|paper)_[A-Za-z0-9][\w-]*$/;

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

function artifactRole(r) {
  return r.association_role || r.role || null;
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

  if (type === 'artifact') {
    // The home snapshot's `resources` rows are artifact-shaped (id = art_*).
    const r = (H.resources || []).find((x) => x.id === id);
    if (!r) return { ...DEAD(id, type), needsFetch: true };
    return {
      id, type, label: r.title || basename(r.path) || 'artifact', route: ROUTE.artifact(id), navigable: true,
      detail: {
        type, path: r.path || '', role: artifactRole(r), updated_at: r.updated_at || r.created_at,
      },
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

  // Lit-review sections and papers: not in the home snapshot — resolve by
  // fetch, but the chip already routes to the lit-review screen.
  if (type === 'litreview_section' || type === 'paper') {
    return {
      ...DEAD(id, type),
      label: TYPE_LABEL[type],
      route: ROUTE[type](id),
      navigable: true,
      needsFetch: true,
    };
  }

  // reflection: never carried in the home snapshot — resolve by fetch.
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
  if (t === 'artifact') {
    return {
      id: refString, type: 'artifact', label: entry.title || basename(entry.path) || 'artifact',
      route: entry.artifact_id ? ROUTE.artifact(entry.artifact_id) : null, navigable: !!entry.artifact_id,
      detail: { type: 'artifact', path: entry.path || '', role: entry.role },
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
  if (t === 'reflection') {
    return {
      id: refString, type: 'reflection', label: entry.title || 'reflection', navigable: false,
      detail: { type: 'reflection', status: entry.status, decision: entry.decision },
    };
  }
  if (t === 'litreview_section') {
    return {
      id: refString, type: 'litreview_section', label: entry.title || 'lit review section',
      route: ROUTE.litreview_section(refString), navigable: true,
      detail: { type: 'litreview_section', title: entry.title || '', tldr: entry.tldr || '' },
    };
  }
  if (t === 'paper') {
    return {
      id: refString, type: 'paper', label: clamp(entry.title, 44) || 'paper',
      route: ROUTE.paper(refString), navigable: true,
      detail: { type: 'paper', title: entry.title || '', url: entry.url || '', year: entry.year || '' },
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
    } else if (type === 'artifact') {
      const s = await api.listArtifacts(pid);
      const a = (s?.artifacts || []).find((x) => x.id === id);
      if (a) {
        out = {
          id, type, label: a.title || basename(a.path) || shortId(id),
          route: ROUTE.artifact(id), navigable: true,
          detail: { type, path: a.path || '', role: a.role, updated_at: a.created_at },
        };
      }
    } else if (type === 'reflection') {
      const s = await api.getReflection(pid, id);
      const w = s || {};
      out = {
        id, type, label: 'reflection', navigable: false,
        detail: { type, status: w.status, decision: w.decision },
      };
    } else if (type === 'litreview_section' || type === 'paper') {
      const s = await api.getLitReview(pid);
      if (type === 'litreview_section') {
        const sec = [s?.summary, ...(s?.sections || [])].find((x) => x && x.id === id);
        if (sec) {
          out = {
            id, type, label: sec.title || 'lit review section',
            route: ROUTE.litreview_section(id), navigable: true,
            detail: { type, title: sec.title || '', tldr: sec.tldr || '' },
          };
        }
      } else {
        const p = (s?.papers || []).find((x) => x.id === id);
        if (p) {
          out = {
            id, type, label: clamp(p.title, 44) || shortId(id),
            route: ROUTE.paper(id), navigable: true,
            detail: { type, title: p.title || '', url: p.url || '', year: p.year || '', authors: p.authors || [] },
          };
        }
      }
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
