import { useCallback, useSyncExternalStore } from 'react';

/**
 * Expansion state for the resources FileTree, lifted out of the component so
 * it survives remounts — drawer toggles, transient empty payloads during
 * project switches — and so every tree instance (desktop sidebar, mobile)
 * shares one truth. Same module-store pattern as useTheme/usePanelWidth.
 *
 * Per project: `expanded` is the set of open folder paths (replaced, never
 * mutated, so useSyncExternalStore sees a new snapshot per change) and
 * `seenTops` remembers which top-level folders have ever appeared — new ones
 * auto-expand on first sight, seen ones keep whatever the user chose.
 */
const byProject = new Map(); // pid -> { expanded: Set<path>, seenTops: Set<path>, handledSelection }
const listeners = new Set();

function bucket(pid) {
  const key = pid || '_';
  let b = byProject.get(key);
  if (!b) {
    b = { expanded: new Set(), seenTops: new Set(), handledSelection: null };
    byProject.set(key, b);
  }
  return b;
}

function emit() { for (const fn of listeners) fn(); }

export function useExpandedFolders(pid) {
  return useSyncExternalStore(
    useCallback((fn) => { listeners.add(fn); return () => listeners.delete(fn); }, []),
    () => bucket(pid).expanded,
  );
}

export function togglePath(pid, path) {
  const b = bucket(pid);
  const next = new Set(b.expanded);
  if (next.has(path)) next.delete(path);
  else next.add(path);
  b.expanded = next;
  emit();
}

export function expandPaths(pid, paths) {
  const b = bucket(pid);
  let next = b.expanded;
  for (const p of paths) {
    if (!next.has(p)) {
      if (next === b.expanded) next = new Set(next);
      next.add(p);
    }
  }
  if (next !== b.expanded) {
    b.expanded = next;
    emit();
  }
}

// The ancestors-of-selection expansion must run once per selection, not once
// per FileTree instance — a ref would replay it on every remount, reopening
// folders the user collapsed. So the "already handled" memory lives here.
export function selectionHandled(pid, id) {
  return bucket(pid).handledSelection === id;
}

export function markSelectionHandled(pid, id) {
  bucket(pid).handledSelection = id;
}

export function autoExpandNewTops(pid, tops) {
  const b = bucket(pid);
  const fresh = [...tops].filter(p => !b.seenTops.has(p));
  if (fresh.length === 0) return;
  for (const p of fresh) b.seenTops.add(p);
  expandPaths(pid, fresh);
}
