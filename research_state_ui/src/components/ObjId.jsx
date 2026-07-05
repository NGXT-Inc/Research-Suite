import { useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import { useProjectStore } from '../store/useProjectStore';
import { resolveEntity, fetchEntity, entityType } from '../utils/entityResolve';
import { useEntityHover } from './useEntityHover';
import EntityHoverCard from './EntityHoverCard';

/**
 * Small monospace ID chip for console-dialect id columns. Shortens
 * `exp_abc12345…` to a stable suffix the eye can scan, and — unlike a bare
 * label — reveals the same entity detail card as EntityChip on hover. Stays a
 * plain inline span (no tab stop): the entity is reachable by its row/name
 * elsewhere; the card is a mouse-hover enhancement.
 */
export default function ObjId({ id, strong = false, accent = false, className = '' }) {
  const home = useProjectStore((s) => s.home);
  const pid = useProjectStore((s) => s.projectId);
  const [fetched, setFetched] = useState(null);
  const [loading, setLoading] = useState(false);

  const resolved = fetched || (id ? resolveEntity(id, home) : null);

  const load = useCallback(() => {
    if (fetched || loading || !resolved?.needsFetch) return;
    setLoading(true);
    fetchEntity(id, pid).then(setFetched).finally(() => setLoading(false));
  }, [fetched, loading, resolved?.needsFetch, id, pid]);

  const {
    enabled, open, isPositioned, setReference, setFloating, floatingStyles,
    getReferenceProps, getFloatingProps,
  } = useEntityHover({ load });

  if (!id) return null;

  // Only recognised research entities get a hover card; a sandbox/tool-call id
  // (unknown prefix) stays a plain id chip so it never shows a misleading
  // "not found" card.
  const hasCard = !!entityType(id);
  const short = id.length > 14 ? `${id.slice(0, 4)}…${id.slice(-6)}` : id;
  const cls = ['obj-id'];
  if (strong) cls.push('obj-id--strong');
  if (accent) cls.push('obj-id--accent');
  if (className) cls.push(className);

  const card = hasCard && enabled && open && resolved
    ? createPortal(
        <div
          className="ehover"
          ref={setFloating}
          style={{ ...floatingStyles, visibility: isPositioned ? 'visible' : 'hidden' }}
          {...getFloatingProps()}
        >
          <EntityHoverCard resolved={resolved} loading={loading} />
        </div>,
        document.body,
      )
    : null;

  // With our hover card, use aria-label instead of the native `title` — the
  // OS-drawn grey tooltip otherwise appears over the card. Non-entity ids (no
  // card) keep `title` as their only way to reveal the full id on hover.
  const idProps = hasCard
    ? getReferenceProps({ 'aria-label': id, className: cls.join(' ') })
    : { title: id, className: cls.join(' ') };

  return (
    <>
      <span ref={hasCard ? setReference : undefined} {...idProps}>{short}</span>
      {card}
    </>
  );
}
