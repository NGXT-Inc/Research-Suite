import { create } from 'zustand';

/**
 * Kin highlighting — hovering a beat softly lights up other beats that touch
 * the same claims. A tiny dedicated store instead of lifted React state so
 * each beat subscribes to its OWN boolean (`isKin`) and only the handful of
 * rows whose status flips re-render, not the whole story tree per mousemove.
 */
export const useKinHover = create(set => ({
  ids: null, // claim ids under the pointer, or null
  setIds: ids => set(state => (state.ids === ids ? state : { ids })),
}));

export function clearKinHover() {
  useKinHover.getState().setIds(null);
}

/** Subscribe to "is this beat kin to the hovered one?" — stable boolean. */
export function useIsKin(claimIds) {
  return useKinHover(s => Boolean(
    s.ids && s.ids.length > 0 && claimIds.length > 0
    && s.ids.some(id => claimIds.includes(id)),
  ));
}
