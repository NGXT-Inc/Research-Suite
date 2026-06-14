import { useCallback, useRef, useSyncExternalStore } from 'react';

/**
 * Shared, draggable width for the graph node-detail panel.
 *
 * The figure and logic graphs each render their own .fig-body split, and the
 * project-synthesis panel reuses the logic graph — so the width lives in a
 * tiny module-level store (same pattern as useTheme) rather than per-component
 * state. That keeps every panel in lockstep and persists the user's choice
 * across reloads.
 *
 * The width drives a CSS custom property (--fig-panel-w) that the .fig-body
 * grid and the resize handle both read, so the media-query stack-on-mobile
 * rule can still override the grid wholesale (an inline grid-template can't).
 */
const KEY = 'rsui:figPanelW';
const MIN = 300;
const DEFAULT = 380;

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function load() {
  try {
    const v = parseInt(localStorage.getItem(KEY), 10);
    return Number.isFinite(v) ? Math.max(MIN, v) : DEFAULT;
  } catch { return DEFAULT; }
}

let width = load();
const listeners = new Set();

function setWidth(w) {
  width = w;
  for (const fn of listeners) fn();
}

export function usePanelWidth() {
  const value = useSyncExternalStore(
    useCallback((fn) => { listeners.add(fn); return () => listeners.delete(fn); }, []),
    () => width,
  );
  const drag = useRef(null);

  const onMove = useCallback((e) => {
    const s = drag.current;
    if (!s) return;
    // The panel is the RIGHT column, so it widens as the pointer moves left.
    setWidth(clamp(s.startW + (s.startX - e.clientX), MIN, s.maxW));
  }, []);

  const onUp = useCallback(() => {
    drag.current = null;
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { localStorage.setItem(KEY, String(Math.round(width))); } catch { /* best-effort */ }
  }, [onMove]);

  const startResize = useCallback((e) => {
    e.preventDefault();
    const body = e.currentTarget.closest('.fig-body');
    const bodyW = body ? body.clientWidth : 960;
    // Never let the panel eat the whole canvas: leave the graph ~300px.
    drag.current = { startX: e.clientX, startW: width, maxW: Math.max(MIN, bodyW - 300) };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, [onMove, onUp]);

  return { width: value, startResize };
}
