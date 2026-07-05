import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useFloating, offset, flip, shift, autoUpdate } from '@floating-ui/react';

// The card is a pointer/keyboard affordance, never opened on touch (a hover card
// sticks after a tap; the chip should behave as a plain link there). Evaluated
// once — a device with a mouse at load reports hover/fine and stays enabled.
const CAN_HOVER = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(hover: hover) and (pointer: fine)').matches;

// Hover-intent delay before opening; grace period before closing. The grace
// absorbs both real-hand micro-jitter at the chip's edge and the travel across
// the 6px offset gap between chip and card — no safePolygon geometry needed.
const OPEN_DELAY = 180;
const CLOSE_GRACE = 160;

// Middleware are stateless config objects, so one module-level array serves
// every chip and its identity never changes across renders.
const MIDDLEWARE = [offset(6), flip({ padding: 8 }), shift({ padding: 8 })];

// :focus-visible tells keyboard focus (open the card) from mouse-click focus
// (hover already handles it). Old engines without the selector open on any
// focus — the safe a11y default.
function isFocusVisible(el) {
  try { return el.matches(':focus-visible'); } catch { return true; }
}

/**
 * Hover-card wiring: floating-ui for POSITIONING ONLY (offset/flip/shift +
 * autoUpdate), with a hand-rolled open/close state machine on top.
 *
 * Why not floating-ui's useHover/safePolygon interaction layer: its close
 * logic lives in effects and document-mousemove closures that capture the
 * reference DOM node. This app re-renders every ~3s (home poll) and EntityChip
 * swaps its rendered element (<button> → <Link>) when the lazy fetch resolves
 * mid-hover, so those closures ended up holding a detached node — contains()
 * always false, getBoundingClientRect() all zeros — and the next real-mouse
 * tremor closed the card instantly. Headless pointers don't tremble, which is
 * why automated checks kept passing while a human's hover died.
 *
 * This implementation is immune by construction:
 *  - All mutable state (timers, open flag, latest chip/card nodes, load fn)
 *    lives in refs — background re-renders can't reset or recapture anything.
 *  - Enter/leave handlers ride React props, so an element-type swap re-binds
 *    them atomically in the same commit; chipRef always holds the live node.
 *  - While open, one capture-phase document `mouseover` listener is the
 *    authority: pointer inside chip-or-card cancels the close, anywhere else
 *    starts the CLOSE_GRACE countdown. It re-reads the refs on every event, so
 *    it can never go stale, and any missed enter/leave pair is self-healing.
 *  - Escape closes (keyboard parity with the old useDismiss); leaving the
 *    window closes; blur closes.
 * `load` fires only on confirmed hover intent (after OPEN_DELAY) or keyboard
 * focus — never on render — preserving the zero-fetch-until-hover guarantee.
 */
export function useEntityHover({ load } = {}) {
  const [open, setOpen] = useState(false);
  const cardId = useId();

  const chipRef = useRef(null);
  const cardRef = useRef(null);
  const openTimer = useRef(-1);
  const closeTimer = useRef(-1);
  const openRef = useRef(false);
  openRef.current = open;
  // Latest-ref so callers need no useCallback discipline and a mid-hover
  // identity change of `load` (e.g. its setState deps) is irrelevant here.
  const loadRef = useRef(load);
  loadRef.current = load;

  const { refs, floatingStyles, isPositioned } = useFloating({
    open,
    placement: 'bottom-start',
    // Fixed strategy positions correctly even inside transformed ancestors
    // (e.g. the logic-DAG canvas).
    strategy: 'fixed',
    middleware: MIDDLEWARE,
    whileElementsMounted: autoUpdate,
  });

  // Merge our always-current node refs with floating-ui's positioning refs.
  // floating-ui's setters are identity-stable, so these are too — React only
  // re-runs ref callbacks when their identity changes.
  const setReference = useCallback((node) => {
    chipRef.current = node;
    refs.setReference(node);
  }, [refs.setReference]); // eslint-disable-line react-hooks/exhaustive-deps
  const setFloating = useCallback((node) => {
    cardRef.current = node;
    refs.setFloating(node);
  }, [refs.setFloating]); // eslint-disable-line react-hooks/exhaustive-deps

  // The while-open keeper. Keyed only on `open`, self-contained, reads refs —
  // re-renders never touch it; StrictMode's double-run attaches idempotently.
  useEffect(() => {
    if (!open || !CAN_HOVER) return undefined;
    const doc = document;
    const onOver = (e) => {
      const t = e.target;
      const inside = (chipRef.current && chipRef.current.contains(t))
        || (cardRef.current && cardRef.current.contains(t));
      clearTimeout(closeTimer.current);
      if (!inside) {
        closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_GRACE);
      }
    };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    const onWindowLeave = () => setOpen(false);
    doc.addEventListener('mouseover', onOver, true);
    doc.addEventListener('keydown', onKey, true);
    doc.documentElement.addEventListener('mouseleave', onWindowLeave);
    return () => {
      doc.removeEventListener('mouseover', onOver, true);
      doc.removeEventListener('keydown', onKey, true);
      doc.documentElement.removeEventListener('mouseleave', onWindowLeave);
    };
  }, [open]);

  // Clear pending timers if the chip unmounts (navigation, list removal).
  useEffect(() => () => {
    clearTimeout(openTimer.current);
    clearTimeout(closeTimer.current);
  }, []);

  const scheduleOpen = () => {
    clearTimeout(closeTimer.current);
    if (openRef.current) return;
    clearTimeout(openTimer.current);
    openTimer.current = window.setTimeout(() => {
      loadRef.current?.();
      setOpen(true);
    }, OPEN_DELAY);
  };
  const scheduleClose = () => {
    clearTimeout(openTimer.current);
    if (!openRef.current) return;
    clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_GRACE);
  };

  const getReferenceProps = (extra = {}) => {
    if (!CAN_HOVER) return extra;
    return {
      ...extra,
      'aria-describedby': open ? cardId : extra['aria-describedby'],
      onMouseEnter(e) { extra.onMouseEnter?.(e); scheduleOpen(); },
      onMouseLeave(e) { extra.onMouseLeave?.(e); scheduleClose(); },
      onFocus(e) {
        extra.onFocus?.(e);
        if (isFocusVisible(e.target)) {
          clearTimeout(openTimer.current);
          clearTimeout(closeTimer.current);
          loadRef.current?.();
          setOpen(true);
        }
      },
      onBlur(e) {
        extra.onBlur?.(e);
        clearTimeout(openTimer.current);
        clearTimeout(closeTimer.current);
        setOpen(false);
      },
    };
  };

  const getFloatingProps = (extra = {}) => ({
    ...extra,
    id: cardId,
    role: 'tooltip',
    onMouseEnter(e) { extra.onMouseEnter?.(e); clearTimeout(closeTimer.current); },
    onMouseLeave(e) { extra.onMouseLeave?.(e); scheduleClose(); },
  });

  return {
    enabled: CAN_HOVER,
    open,
    isPositioned,
    setReference,
    setFloating,
    floatingStyles,
    getReferenceProps,
    getFloatingProps,
  };
}
