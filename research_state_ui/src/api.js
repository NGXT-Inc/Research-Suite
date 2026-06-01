/**
 * Thin fetch wrapper for the research_plugin HTTP API (UI_API.md).
 *
 * In dev, Vite proxies /api and /health to 127.0.0.1:8787. In production
 * the UI is intended to run alongside the backend on the same host; allow
 * an override via VITE_API_BASE.
 */
const BASE = import.meta.env.VITE_API_BASE || '';

async function request(path, { method = 'GET', body, signal } = {}) {
  const init = { method, signal, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${path}`, init);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!res.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${res.status} on ${method} ${path}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  health: () => request('/health'),

  // Projects
  listProjects: () => request('/api/projects'),
  createProject: ({ name, summary }) => request('/api/projects', { method: 'POST', body: { name, summary: summary || '' } }),
  getProject: (pid) => request(`/api/projects/${encodeURIComponent(pid)}`),
  patchProject: (pid, patch) => request(`/api/projects/${encodeURIComponent(pid)}`, { method: 'PATCH', body: patch }),
  getHome: (pid, signal) => request(`/api/projects/${encodeURIComponent(pid)}/home`, { signal }),
  getStatus: (pid, experimentId) => {
    const q = experimentId ? `?experiment_id=${encodeURIComponent(experimentId)}` : '';
    return request(`/api/projects/${encodeURIComponent(pid)}/status${q}`);
  },

  // Claims
  listClaims: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/claims`),
  createClaim: (pid, { statement, scope, confidence }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/claims`, {
      method: 'POST',
      body: { statement, scope: scope || '', confidence: confidence || 'medium' },
    }),
  getClaim: (pid, cid) => request(`/api/projects/${encodeURIComponent(pid)}/claims/${encodeURIComponent(cid)}`),

  // Experiments
  listExperiments: (pid, opts = {}) => {
    const q = opts.status ? `?status=${encodeURIComponent(opts.status)}` : '';
    return request(`/api/projects/${encodeURIComponent(pid)}/experiments${q}`);
  },
  listExperimentsView: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/experiments/view`),
  createExperiment: (pid, { intent, claim_ids }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments`, {
      method: 'POST',
      body: { intent, claim_ids: claim_ids || [] },
    }),
  getExperiment: (pid, eid) => request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}`),
  getExperimentStatus: (pid, eid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/status`),
  transitionExperiment: (pid, eid, transition, evidence) =>
    request(`/api/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}/transition`, {
      method: 'POST',
      body: { transition, ...(evidence ? { evidence } : {}) },
    }),

  // Resources
  listResources: (pid, opts = {}) => {
    const q = opts.kind ? `?kind=${encodeURIComponent(opts.kind)}` : '';
    return request(`/api/projects/${encodeURIComponent(pid)}/resources${q}`);
  },
  resourceTree: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/resources/tree`),
  registerResource: (pid, { path, kind, title }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources`, {
      method: 'POST',
      body: { path, kind, ...(title ? { title } : {}) },
    }),
  associateResource: (pid, rid, { target_type, target_id, role }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/associate`, {
      method: 'POST',
      body: { target_type, target_id, role },
    }),
  getResource: (pid, rid) => request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}`),
  getResourceContent: (pid, rid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/content`),
  resourceFileUrl: (pid, rid) =>
    `${BASE}/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/file`,

  // Versioning (shadow-Git-backed). Resources carry version metadata directly
  // (current_version_id, current_version, associations[].version_id), but for
  // history / historical content / diffs the UI hits these endpoints.
  getResourceHistory: (pid, rid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/history`),
  getResourceVersionContent: (pid, rid, vid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/versions/${encodeURIComponent(vid)}/content`),
  getResourceVersionDiff: (pid, rid, toVid, fromVid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/resources/${encodeURIComponent(rid)}/versions/${encodeURIComponent(toVid)}/diff?from_version_id=${encodeURIComponent(fromVid)}`),

  // Reviews
  listReviews: (pid, target = {}) => {
    const params = new URLSearchParams();
    if (target.target_type) params.set('target_type', target.target_type);
    if (target.target_id) params.set('target_id', target.target_id);
    const q = params.toString();
    return request(`/api/projects/${encodeURIComponent(pid)}/reviews${q ? '?' + q : ''}`);
  },
  requestReview: (pid, { target_type, target_id, role, reason }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/reviews/request`, {
      method: 'POST',
      body: { target_type, target_id, role, reason: reason || '' },
    }),

  // Events
  listEvents: (pid, limit = 100) =>
    request(`/api/projects/${encodeURIComponent(pid)}/events?limit=${limit}`),

  // Activity — workspace-scoped (http.request + tool.call telemetry).
  // Returns { activity_log, events, summary } oldest-first. The optional
  // `source` filter is applied server-side BEFORE `limit` so a request for
  // 300 mcp events returns 300 mcp events, not 300 mixed events of which mcp
  // is a sliver.
  listActivity: (limit = 200, source = null) => {
    const params = new URLSearchParams();
    params.set('limit', String(limit));
    if (source && source !== 'all') params.set('source', source);
    return request(`/api/activity?${params.toString()}`);
  },

  // Jobs (Ray-backed; see jobs.py)
  listJobs: (pid, { experimentId, status } = {}) => {
    const params = new URLSearchParams();
    if (experimentId) params.set('experiment_id', experimentId);
    if (status) params.set('status', status);
    const q = params.toString();
    return request(`/api/projects/${encodeURIComponent(pid)}/jobs${q ? '?' + q : ''}`);
  },
  submitJob: (pid, { experiment_id, command, cwd, expected_outputs }) =>
    request(`/api/projects/${encodeURIComponent(pid)}/jobs`, {
      method: 'POST',
      body: {
        experiment_id,
        command,
        cwd: cwd || '.',
        expected_outputs: expected_outputs || [],
      },
    }),
  getJob: (pid, jid) => request(`/api/projects/${encodeURIComponent(pid)}/jobs/${encodeURIComponent(jid)}`),
  getJobLogs: (pid, jid, tail = 200) =>
    request(`/api/projects/${encodeURIComponent(pid)}/jobs/${encodeURIComponent(jid)}/logs?tail=${tail}`),
  getJobOutputs: (pid, jid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/jobs/${encodeURIComponent(jid)}/outputs`),
  cancelJob: (pid, jid) =>
    request(`/api/projects/${encodeURIComponent(pid)}/jobs/${encodeURIComponent(jid)}/cancel`, { method: 'POST' }),
  jobsHealth: (pid) => request(`/api/projects/${encodeURIComponent(pid)}/jobs/health`),
};
