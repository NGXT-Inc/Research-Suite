import { create } from 'zustand';
import { api } from '../api';

const PROJECT_KEY = 'rsui:projectId';

function loadInitialProjectId() {
  try {
    return localStorage.getItem(PROJECT_KEY) || null;
  } catch { return null; }
}

export const useProjectStore = create((set, get) => ({
  // Identity
  projectId: loadInitialProjectId(),
  projects: [],          // [{id, name, summary, created_at}]
  projectsLoaded: false,

  // Bootstrap state
  bootError: null,

  // Live snapshot from GET /home
  home: null,            // {project, claims, experiments, resources, reviews, recent_events, stats, workflow, active_experiment, active_experiments, active_processes}
  sandboxes: [],         // project-wide sandbox list from GET /sandboxes (one per experiment)
  events: [],            // longer event window from GET /events?limit=500 — powers dashboard sparklines
  lastSyncedAt: null,    // epoch ms of last successful refresh
  lastSyncError: null,
  isPolling: false,

  setProjectId(pid) {
    try {
      if (pid) localStorage.setItem(PROJECT_KEY, pid);
      else localStorage.removeItem(PROJECT_KEY);
    } catch {}
    set({ projectId: pid, home: null, sandboxes: [], events: [], lastSyncedAt: null, lastSyncError: null });
  },

  async loadProjects() {
    try {
      const data = await api.listProjects();
      const list = data.projects || [];
      set({ projects: list, projectsLoaded: true });
      // If the persisted project id is gone, fall back to the first one.
      const current = get().projectId;
      if (current && !list.some(p => p.id === current)) {
        get().setProjectId(list[0]?.id || null);
      } else if (!current && list.length > 0) {
        get().setProjectId(list[0].id);
      }
      return list;
    } catch (err) {
      set({ bootError: err.message, projectsLoaded: true });
      return [];
    }
  },

  async createProject({ name, summary, repo_root }) {
    const created = await api.createProject({ name, summary, repo_root });
    const projectRow = created.project || created;
    await get().loadProjects();
    get().setProjectId(projectRow.id);
    await get().refreshHome();
    return projectRow;
  },

  async patchProject(pid, patch) {
    const updated = await api.patchProject(pid, patch);
    const row = updated.project || updated;
    set((state) => ({
      projects: state.projects.map(p => (p.id === pid ? { ...p, ...row } : p)),
    }));
    if (get().projectId === pid) await get().refreshHome();
    return row;
  },

  async refreshHome() {
    const pid = get().projectId;
    if (!pid) return null;
    // Fetch /home, /sandboxes, and a deeper /events window in parallel.
    // /home's recent_events is capped at ~25, too few for the Events page;
    // the deeper window powers anything that needs ≥1h of history.
    try {
      const [home, sandboxesResp, eventsResp] = await Promise.all([
        api.getHome(pid),
        api.listSandboxes(pid).catch(() => ({ sandboxes: [] })),
        api.listEvents(pid, 500).catch(() => ({ events: [] })),
      ]);
      const sandboxes = Array.isArray(sandboxesResp?.sandboxes) ? sandboxesResp.sandboxes : [];
      const events = Array.isArray(eventsResp?.events) ? eventsResp.events : [];
      set({ home, sandboxes, events, lastSyncedAt: Date.now(), lastSyncError: null });
      return home;
    } catch (err) {
      set({ lastSyncError: err.message });
      return null;
    }
  },

  setPolling(on) { set({ isPolling: on }); },
}));

/**
 * Selector helpers — keep components from re-rendering on unrelated slices.
 *
 * NOTE: every selector must return an *identity-stable* reference when the
 * underlying slice is missing. Zustand's getSnapshot contract requires this:
 * returning a fresh `[]` / `{}` every call would re-trigger React's external
 * store and cause "Maximum update depth exceeded".
 */
const EMPTY_OBJ = Object.freeze({});
const EMPTY_ARR = Object.freeze([]);

export const selectStats = (s) => s.home?.stats || EMPTY_OBJ;
export const selectClaims = (s) => s.home?.claims || EMPTY_ARR;
export const selectExperiments = (s) => s.home?.experiments || EMPTY_ARR;
export const selectResources = (s) => s.home?.resources || EMPTY_ARR;
// Server returns reviews as { requests, reviews } on /home and on /reviews.
export const selectReviewRequests = (s) => {
  const r = s.home?.reviews;
  if (r && !Array.isArray(r) && typeof r === 'object') return r.requests || EMPTY_ARR;
  return EMPTY_ARR;
};
export const selectEvents = (s) => s.home?.recent_events || EMPTY_ARR;
export const selectActiveExperiments = (s) => s.home?.active_experiments || EMPTY_ARR;
export const selectActiveProcesses = (s) => s.home?.active_processes || EMPTY_ARR;
export const selectSandboxes = (s) => s.sandboxes || EMPTY_ARR;
export const selectEventsAll = (s) => s.events || EMPTY_ARR;
export const selectProject = (s) => s.home?.project || null;
