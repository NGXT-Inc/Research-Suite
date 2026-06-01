import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

/**
 * Smooth-scroll to the element whose id matches `location.hash` after the
 * route changes. Useful for cross-page deep links like
 * `/experiments/:id#execution` — the destination page renders, then this
 * hook locates the target section and scrolls it into view.
 *
 * Sections often mount after async data loads, so the lookup retries briefly
 * (cap ~1s) until the element appears.
 *
 * Pass extra dependencies in `deps` to re-run the scroll-attempt loop once
 * that data arrives (e.g. once the experiment object is loaded). Critically:
 * the scroll only fires ONCE per hash value. Polling-driven re-renders of
 * the deps won't yank the viewport back if the user has scrolled away.
 */
export function useScrollToHash(deps = []) {
  const { hash } = useLocation();
  const scrolledForRef = useRef(null);
  useEffect(() => {
    if (!hash) return undefined;
    if (scrolledForRef.current === hash) return undefined;
    const id = hash.slice(1);
    let attempts = 0;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const el = document.getElementById(id);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        scrolledForRef.current = hash;
        return;
      }
      if (attempts++ < 20) setTimeout(tick, 50);
    };
    tick();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, ...deps]);
}

/**
 * Map a workflow gate / experiment status to the section anchor id used on
 * the experiment detail page. Returns null when no obvious section matches.
 */
export function gateToSectionId(gate) {
  const g = String(gate || '').toLowerCase();
  // Order matters: design_review must hit 'design', experiment_review must
  // hit 'outcomes', so check the longer / more specific tokens first.
  if (g.includes('experiment_review') || g === 'terminal' || g === 'complete') return 'outcomes';
  if (g.includes('design')) return 'design';
  if (g.includes('execution') || g === 'ready_to_run' || g === 'running') return 'execution';
  if (g === 'planned' || g === 'idle') return 'design';
  return null;
}
