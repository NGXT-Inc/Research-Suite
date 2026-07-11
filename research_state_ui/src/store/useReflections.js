import { useEffect } from 'react';
import { create } from 'zustand';
import { api } from '../api';

/**
 * One shared poll of GET /reflections per project, however many components
 * are watching (the Home page alone has two: the Research Story derives its
 * act breaks from it, the synthesis panel renders the waves). Consumers call
 * useReflections(projectId) and read the latest payload; the store keeps a
 * single interval alive while anyone subscribes and drops the previous
 * project's data the moment the project changes, so a slow first fetch can
 * never show one project's waves against another project's records.
 */

const POLL_MS = 8000;

export const useReflectionsStore = create((set, get) => ({
  pid: null,
  payload: null, // { syntheses, current, signal } — server shape, untouched

  _watchers: 0,
  _timer: null,
  _epoch: 0, // bumps on project switch; stale fetches check it before writing

  async _fetch() {
    const { pid, _epoch } = get();
    if (!pid) return;
    try {
      const payload = await api.getSyntheses(pid);
      if (get()._epoch !== _epoch) return; // project changed mid-flight
      set(state => (
        JSON.stringify(state.payload) === JSON.stringify(payload)
          ? state
          : { payload }
      ));
    } catch {
      // Non-fatal: consumers render without waves until the next tick.
    }
  },

  _sync(pid) {
    if (get().pid !== pid) {
      set(state => ({ pid, payload: null, _epoch: state._epoch + 1 }));
    }
    if (get()._watchers > 0 && !get()._timer && pid) {
      get()._fetch();
      set({ _timer: setInterval(() => get()._fetch(), POLL_MS) });
    }
  },

  _acquire(pid) {
    set(state => ({ _watchers: state._watchers + 1 }));
    get()._sync(pid);
  },

  _release() {
    set(state => ({ _watchers: Math.max(0, state._watchers - 1) }));
    if (get()._watchers === 0 && get()._timer) {
      clearInterval(get()._timer);
      set({ _timer: null });
    }
  },
}));

export function useReflections(projectId) {
  const payload = useReflectionsStore(s => (s.pid === projectId ? s.payload : null));
  useEffect(() => {
    const store = useReflectionsStore.getState();
    store._acquire(projectId);
    return () => useReflectionsStore.getState()._release();
  }, [projectId]);
  // Keep the poll pointed at the active project even if a stale subscriber
  // lingers through a switch (acquire/release order is not guaranteed).
  useEffect(() => {
    useReflectionsStore.getState()._sync(projectId);
  }, [projectId]);
  return payload;
}

/** Refetch immediately (after a mutation a consumer just performed). */
export function refreshReflections() {
  useReflectionsStore.getState()._fetch();
}
