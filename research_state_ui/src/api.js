/**
 * Thin fetch wrapper for the merv HTTP API (UI_API.md).
 *
 * In dev, Vite proxies /api and /health to 127.0.0.1:8787. In production
 * the UI is intended to run alongside the backend on the same host; allow
 * an override via VITE_API_BASE.
 */
// Resolution order: build-time override, then a runtime dev override
// (localStorage 'rsui:apiBase' — handy for pointing the UI at a working-tree
// daemon on another port; the backend allows cross-origin reads), then the
// same-origin default (Vite dev proxy / production co-hosting).
// Strip any trailing slash so a configured base like "https://host/" doesn't
// produce a double slash ("https://host//api/...") — which the API 404s.
const BASE = (
  import.meta.env.VITE_API_BASE
  || (typeof localStorage !== 'undefined' && localStorage.getItem('rsui:apiBase'))
  || ''
).replace(/\/+$/, '');

// The UI build's wire version, stamped on every request as X-RP-Client-Version
// (the cloud control plane reads it for the compat handshake; local mode
// ignores it). Kept in lockstep with the merv package version.
export const CLIENT_VERSION = '0.0013';

// Bearer token for the hosted control plane. Dormant in local mode: with no
// token configured no Authorization header is sent, and the local backend
// (auth=None) serves every request as the implicit local principal. The live
// Supabase session (hosted sign-in) wins; the localStorage slot remains as a
// dev override and can also hold an rr_sk_ API key.
import { getAuthToken, tryRefreshSession } from './auth';

function authToken() {
  return (
    getAuthToken()
    || import.meta.env.VITE_API_TOKEN
    || (typeof localStorage !== 'undefined' && localStorage.getItem('rsui:apiToken'))
    || ''
  );
}

// True when requests carry a credential — the signal that header-less media
// surfaces (<img>, <iframe>, new-tab links) must switch to blob: URLs.
export function mediaNeedsAuth() {
  return Boolean(authToken());
}

// Media URLs from api helpers are BASE-prefixed absolutes; fetchObjectUrl
// expects the server-relative path.
export function stripApiBase(url) {
  return BASE && url.startsWith(BASE) ? url.slice(BASE.length) : url;
}

// Absolute URL for a server-provided relative media/asset path, prefixed with
// BASE so it resolves against the daemon even when the dev UI points at another
// origin. Exported as shared transport for self-contained feature modules.
export function mediaUrl(relPath) {
  return `${BASE}${relPath}`;
}

// Media-bytes fetch with the same 401 → refresh → replay-once recovery send()
// uses: the short-lived access token can age out between polls, so a lazily
// loaded figure may fire with a stale bearer. No rp:unauthorized here — a dead
// session surfaces through send()'s pollers; a failed media fetch just leaves
// its placeholder.
async function fetchAuthed(relPath, { signal } = {}, retried = false) {
  const init = { headers: { 'X-RP-Client-Version': CLIENT_VERSION }, signal };
  const token = authToken();
  if (token) init.headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${BASE}${relPath}`, init);
  if (res.status === 401 && !retried && getAuthToken() && (await tryRefreshSession())) {
    return fetchAuthed(relPath, { signal }, true);
  }
  if (!res.ok) throw new Error(`HTTP ${res.status} on GET ${relPath}`);
  return res;
}

// Fetch a binary asset WITH auth and return an object URL for use as an <img>
// src. In hosted control mode every route past /health and /api/meta requires
// the Bearer token, but the browser never attaches it to a plain <img src>, so
// bytes that live in the cloud (feed images, link thumbnails) must be loaded
// through fetch() and wrapped in a blob: URL. Works unchanged in local mode
// (no token → same-origin fetch). Caller MUST URL.revokeObjectURL when done.
export async function fetchObjectUrl(relPath, { signal } = {}) {
  const res = await fetchAuthed(relPath, { signal });
  return URL.createObjectURL(await res.blob());
}

// Fetch a text asset WITH auth (same reasoning as fetchObjectUrl — hosted mode
// serves bytes behind the Bearer token) for content that is rendered inline
// rather than referenced by URL, e.g. sandboxed feed embeds mounted through
// <iframe srcdoc>.
export async function fetchAuthedText(relPath, { signal } = {}) {
  return (await fetchAuthed(relPath, { signal })).text();
}

async function send(path, { method = 'GET', body, signal, headers = {} } = {}, retried = false) {
  const init = { method, signal, headers: { 'X-RP-Client-Version': CLIENT_VERSION, ...headers } };
  const token = authToken();
  if (token) init.headers['Authorization'] = `Bearer ${token}`;
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${path}`, init);
  const text = res.status === 304 ? '' : await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!res.ok && res.status !== 304) {
    // A 401 on a Supabase-session request usually just means the access token
    // aged out (tab closed or backgrounded past the ~1h TTL). Refresh the
    // session and replay the request once before treating it as a real
    // auth failure — otherwise a routine expiry logs the user out.
    if (res.status === 401 && !retried && getAuthToken()) {
      if (await tryRefreshSession()) {
        return send(path, { method, body, signal, headers }, true);
      }
    }
    const err = new Error((data && (data.message || data.detail || data.error)) || `HTTP ${res.status} on ${method} ${path}`);
    err.status = res.status;
    err.data = data;
    // Typed codes for the two control-plane gates so callers can react
    // (login prompt / upgrade banner) instead of showing a raw HTTP error.
    // Both are inert in local mode, which never returns 401/426.
    if (res.status === 401) {
      err.code = 'unauthorized';
      // Only unrecoverable 401s (refresh token dead: revoked account, rotated
      // secret) reach here. Wherever the 401 lands (boot, poller, detail
      // panel), AuthGate hears this and re-shows the sign-in instead of a
      // silent data freeze.
      if (typeof window !== 'undefined') window.dispatchEvent(new Event('rp:unauthorized'));
    }
    else if (res.status === 426) err.code = 'client_too_old';
    else if (data && data.error_code) err.code = data.error_code;
    throw err;
  }
  return { res, data };
}

export async function request(path, opts) {
  return (await send(path, opts)).data;
}

// Conditional GET for the polled snapshot endpoints: sends If-None-Match and
// reports a 304 as { notModified: true } so pollers can skip state writes
// entirely (no payload parse, no re-render churn).
export async function conditionalGet(path, { etag, signal } = {}) {
  const { res, data } = await send(path, {
    signal,
    headers: etag ? { 'If-None-Match': etag } : {},
  });
  return {
    notModified: res.status === 304,
    etag: res.headers.get('ETag') || etag || null,
    data,
  };
}

function sandboxPath(pid, eid, sandboxUid, suffix = '') {
  if (sandboxUid) {
    return `/api/projects/${encodeURIComponent(pid)}/sandboxes/${encodeURIComponent(sandboxUid)}${suffix}`;
  }
  return `/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/sandbox${suffix}`;
}

export const api = {
  // Server identity + compat floor (version handshake). Also reports mode and
  // capabilities so hosted-control UIs can hide local data-plane actions.
  getMeta: () => request('/api/meta'),

  // Projects
  listProjects: () => request('/api/projects'),
  createProject: ({ name, summary, repo_root }) => request('/api/projects', {
    method: 'POST',
    body: { name, summary: summary || '', ...(repo_root ? { repo_root } : {}) },
  }),
  patchProject: (pid, patch) => request(`/api/projects/${encodeURIComponent(pid)}`, { method: 'PATCH', body: patch }),
  getHome: (pid, signal) => request(`/api/projects/${encodeURIComponent(pid)}/home`, { signal }),
  // Conditional variants of the three snapshot endpoints refreshHome polls.
  getHomeIfChanged: (pid, etag, signal) =>
    conditionalGet(`/api/projects/${encodeURIComponent(pid)}/home`, { etag, signal }),
  listSandboxesIfChanged: (pid, etag) =>
    conditionalGet(`/api/projects/${encodeURIComponent(pid)}/sandboxes`, { etag }),
  listEventsIfChanged: (pid, limit, etag) =>
    conditionalGet(`/api/projects/${encodeURIComponent(pid)}/events?limit=${limit}`, { etag }),
  // SSE tail of the project's events table. Note: EventSource cannot send the
  // Authorization header, so against a hosted control plane the stream 401s
  // and the client stays on its polling fallback; local mode needs no auth.
  eventStreamUrl: (pid) => `${BASE}/api/projects/${encodeURIComponent(pid)}/events/stream`,

  // Project MCP keys — owner-only (routes require a Supabase browser session,
  // so these fail in local/API-key mode). Mint returns the mk_ secret exactly
  // once: { key:<public record>, secret:"mk_…" }. List → { keys:[…] }; revoke →
  // { key:<public record> }. The public record never carries the secret.
  listProjectKeys: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/keys`),
  createProjectKey: (pid, body = {}) =>
    request(`/api/projects/${encodeURIComponent(pid)}/keys`, { method: 'POST', body }),
  revokeProjectKey: (pid, keyId) =>
    request(`/api/projects/${encodeURIComponent(pid)}/keys/${encodeURIComponent(keyId)}/revoke`, { method: 'POST' }),

  // Claims
  createClaim: (pid, { statement, scope, confidence }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/claims`, {
      method: 'POST',
      body: { statement, scope: scope || '', confidence: confidence || 'medium' },
    }),
  getClaim: (pid, cid) => request(`/api/projects/${encodeURIComponent(pid)}/claims/${encodeURIComponent(cid)}`),

  // Experiments
  createExperiment: (pid, { name, intent, claim_ids }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments`, {
      method: 'POST',
      body: { name, intent, claim_ids: claim_ids || [] },
    }),
  getExperimentStatus: (pid, eid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/status`),
  // Derived figure graph (nodes + edges) for the experiment canvas.
  getExperimentFigure: (pid, eid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/figure`),
  // Agent-authored logic graph (role 'graph') + envelope lint problems.
  getExperimentLogicGraph: (pid, eid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/graph`),
  transitionExperiment: (pid, eid, transition, evidence) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/transition`, {
      method: 'POST',
      body: { transition, ...(evidence ? { evidence } : {}) },
    }),

  // Reflections (project reflection waves).
  // List + staleness/coverage signal for the Home panel. Each entry is the
  // full wave state (roster, artifacts, reviews, reflection_coverage), so the
  // panel drives the whole history off this one call.
  // The whole literature review (summary, sections, papers ledger) in one read.
  getLitReview: (pid, signal) =>
    request(`/api/projects/${encodeURIComponent(pid)}/litreview`, { signal }),
  getLitReviewIfChanged: (pid, etag) =>
    conditionalGet(`/api/projects/${encodeURIComponent(pid)}/litreview`, { etag }),
  getReflections: (pid, signal) =>
    request(`/api/projects/${encodeURIComponent(pid)}/reflections`, { signal }),
  // One wave, fully hydrated (deep-link / single-wave refresh).
  getReflection: (pid, synId, signal) =>
    request(`/api/projects/${encodeURIComponent(pid)}/reflections/${encodeURIComponent(synId)}`, { signal }),
  // The living project logic graph (same payload shape as the experiment one).
  getProjectLogicGraph: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/reflections/current/graph`),
  // The logic graph of ONE specific wave, rendered from the bytes that wave
  // pinned — so a past wave shows faithfully even after a later wave overwrote
  // the living file. Same payload shape as getProjectLogicGraph.
  getReflectionGraph: (pid, synId) =>
    request(`/api/projects/${encodeURIComponent(pid)}/reflections/${encodeURIComponent(synId)}/graph`),

  // Artifacts — typed objects the agent submitted against workflow targets.
  // Read-only here: submission is agent-only (artifact.submit → one-time
  // upload token), so there are no register/associate/delete calls.
  listArtifacts: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/artifacts`),
  // Decoded text ({ content, is_binary, size_bytes, content_type, available }). An
  // artifact id pins exact bytes — resubmission mints a new id, so there is
  // no version parameter.
  getArtifactContent: (pid, aid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/artifacts/${encodeURIComponent(aid)}/content`),
  artifactFileUrl: (pid, aid) =>
    `${BASE}/api/projects/${encodeURIComponent(pid)}/artifacts/${encodeURIComponent(aid)}/file`,
  // rel: a markdown doc's relative image link → the figure bytes submitted
  // alongside it.
  artifactFigureUrl: (pid, aid, rel) =>
    `${BASE}/api/projects/${encodeURIComponent(pid)}/artifacts/${encodeURIComponent(aid)}/figure?rel=${encodeURIComponent(rel)}`,

  // Reviews
  listReviews: (pid, target = {}) => {
    const params = new URLSearchParams();
    if (target.target_type) params.set('target_type', target.target_type);
    if (target.target_id) params.set('target_id', target.target_id);
    const q = params.toString();
    return request(`/api/projects/${encodeURIComponent(pid)}/reviews${q ? '?' + q : ''}`);
  },
  // Events
  listEvents: (pid, limit = 100) =>
    request(`/api/projects/${encodeURIComponent(pid)}/events?limit=${limit}`),

  // Activity ring — project-scoped, cross-project MCP tool-call telemetry.
  // Returns { activity_log, events, summary } oldest-first. This is the source
  // for the merged Traffic & Tool I/O page (/activity): the ring carries every
  // project's tool calls, whereas tool_calls.sqlite only holds the local
  // workspace's. The `source` filter is applied server-side BEFORE `limit`.
  listActivity: (limit = 200, source = null, projectId = null) => {
    const params = new URLSearchParams();
    params.set('limit', String(limit));
    if (source && source !== 'all') params.set('source', source);
    if (projectId) params.set('project_id', projectId);
    return request(`/api/activity?${params.toString()}`);
  },

  // Tool-call I/O analyzer (legacy, sqlite-backed). Per-tool aggregate plus a
  // sortable slice of calls, each drillable to its FULL raw request/response.
  // Local-workspace only — retained for full-payload drill-down.
  toolCallStats: ({ minutes, source, status, tool, projectId, limit = 300, sort = 'ts', order = 'desc' } = {}) => {
    const p = new URLSearchParams();
    if (minutes) p.set('minutes', String(minutes));
    if (source && source !== 'all') p.set('source', source);
    if (status && status !== 'all') p.set('status', status);
    if (tool) p.set('tool', tool);
    if (projectId) p.set('project_id', projectId);
    p.set('limit', String(limit));
    p.set('sort', sort);
    p.set('order', order);
    return request(`/api/debug/tool-calls?${p.toString()}`);
  },
  // Full raw record for one call, with args/result parsed back to native JSON.
  getToolCall: (id) => request(`/api/debug/tool-calls/${encodeURIComponent(id)}`),
  clearToolCalls: () => request(`/api/debug/tool-calls/clear`, { method: 'POST' }),

  // Sandboxes (cloud-backed; agent drives execution over SSH — see sandboxes.py).
  // The UI observes; it does not procure sandboxes (that is an agent MCP action).
  listSandboxes: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/sandboxes`),
  getSandbox: (pid, eid, { sandboxUid = null } = {}) =>
    request(sandboxPath(pid, eid, sandboxUid)),
  // Terminal transcript. Pass { since: cursor } (from the previous response's
  // `cursor`) to fetch only new bytes — the cheap incremental poll. Without
  // `since`, returns the last `tail` bytes (the initial full pull).
  getSandboxTerminal: (pid, eid, { tail = 200000, since = null, sandboxUid = null } = {}) => {
    const p = new URLSearchParams();
    if (since != null) p.set('since', String(since));
    else p.set('tail', String(tail));
    return request(
      `${sandboxPath(pid, eid, sandboxUid, '/terminal')}?${p.toString()}`,
    );
  },
  // Live in-container usage (CPU/RAM/GPU), sampled on demand. Best-effort:
  // returns { available: false } when the sandbox is not running or the sampler
  // came back empty (e.g. a CPU-only image without nvidia-smi).
  getSandboxMetrics: (pid, eid, { sandboxUid = null } = {}) =>
    request(sandboxPath(pid, eid, sandboxUid, '/metrics')),
  // Durable per-experiment metrics from the centralized MLflow ledger — powers
  // the inline ExperimentMetrics panel. Distinct from getSandboxMetrics (live
  // in-container CPU/RAM/GPU, gone once the VM stops). Returns { available,
  // experiments:[{name, runs:[...]}], dashboard_experiment_url?, hint? }.
  getResultsMetrics: (pid, eid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/results/metrics`),
  // Project-wide MLflow: central endpoint + every experiment's runs/metric
  // curves, each with a deep link into the embedded MLflow UI. Powers the
  // dedicated, project-scoped MLflow page. Returns { mlflow:{configured,
  // dashboard_url,...}, experiments:[{experiment_id, name, status, intent,
  // dashboard_experiment_url, metrics:{...results_metrics...}}] }.
  getMlflowOverview: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/mlflow`),
  releaseSandbox: (pid, eid, { sandboxUid = null } = {}) =>
    request(sandboxPath(pid, eid, sandboxUid, '/release'), { method: 'POST' }),
  // Project compute spend from the sandbox-generations ledger (price × runtime,
  // open boxes bill to now) — covers terminated fleets, unlike listSandboxes.
  // Returns { total_usd, total_hours, unpriced_hours, generations,
  // open_generations, burn_usd_per_hour, by_experiment:[...], by_hardware:[...],
  // daily:[{date, usd, hours}] }.
  getComputeCost: (pid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/compute-cost`),

  // Storage — long-term heavy-file ledger (datasets/models preserved off-repo in
  // S3-compatible storage, R2 first). The UI browses + manages lifecycle; bytes
  // are produced and saved by the agent via the storage.* MCP tools, never
  // uploaded from here. A 404 means the backend storage API isn't present yet.
  listStorage: (pid, { kind, status, name, includeExpired } = {}) => {
    const p = new URLSearchParams();
    if (kind && kind !== 'all') p.set('kind', kind);
    if (status && status !== 'all') p.set('status', status);
    if (name) p.set('name', name);
    if (includeExpired) p.set('include_expired', '1');
    const q = p.toString();
    return request(`/api/projects/${encodeURIComponent(pid)}/storage${q ? '?' + q : ''}`);
  },
  getStorageObject: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}`),
  // Mint a short-lived presigned download URL; the access also bumps the
  // object's 60-day TTL (a fetch means "still in use").
  storageDownloadLink: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}/download`, { method: 'POST' }),
  pinStorage: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}/pin`, { method: 'POST' }),
  unpinStorage: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}/unpin`, { method: 'POST' }),
  renewStorage: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}/renew`, { method: 'POST' }),
  deleteStorage: (pid, id) =>
    request(`/api/projects/${encodeURIComponent(pid)}/storage/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // Personal Hugging Face token (write-only): sets/clears the token used to
  // reach gated models inside your sandboxes. The value is never read back.
  setHfToken: (token) => request('/api/user/hf-token', { method: 'PUT', body: { token } }),
  clearHfToken: () => request('/api/user/hf-token', { method: 'DELETE' }),
};
