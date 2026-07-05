import { useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { useProjectStore, projectPath } from '../store/useProjectStore';
import { resolveEntity, fetchEntity, TYPE_GLYPH } from '../utils/entityResolve';
import { useEntityHover } from './useEntityHover';
import EntityHoverCard from './EntityHoverCard';

/**
 * A compact link chip for any research entity: a type glyph + display name that
 * clips with an ellipsis, navigating to the entity's page on click (or a plain
 * non-navigating chip for types with no detail page — reviews, reflections).
 * Hover/focus reveals a detail card, since the chip itself clips the name.
 *
 * `seed` lets a caller that already resolved the id (e.g. the logic graph's
 * server-provided ref_index) skip both the snapshot lookup and any fetch.
 */
export default function EntityChip({ id, label: labelOverride, seed = null, compact = false, className = '' }) {
  const home = useProjectStore((s) => s.home);
  const pid = useProjectStore((s) => s.projectId);
  const [fetched, setFetched] = useState(null);
  const [loading, setLoading] = useState(false);

  const resolved = seed || fetched || resolveEntity(id, home);

  // Fetch only on the first hover-intent, and only when the snapshot missed —
  // a report full of ids must cost zero requests until one is hovered.
  const load = useCallback(() => {
    if (seed || fetched || loading || !resolved.needsFetch) return;
    setLoading(true);
    fetchEntity(id, pid).then(setFetched).finally(() => setLoading(false));
  }, [seed, fetched, loading, resolved.needsFetch, id, pid]);

  const {
    enabled, open, isPositioned, setReference, setFloating, floatingStyles,
    getReferenceProps, getFloatingProps,
  } = useEntityHover({ load });

  if (!id) return null;

  const glyph = TYPE_GLYPH[resolved.type] || '•';
  const label = labelOverride || resolved.label;
  const cls = ['echip'];
  if (compact) cls.push('echip--compact');
  if (!resolved.navigable) cls.push('echip--static');
  if (resolved.unresolved) cls.push('echip--dead');
  if (className) cls.push(className);

  const inner = (
    <>
      <span className="echip-glyph" aria-hidden="true">{glyph}</span>
      <span className={`echip-name${resolved.unresolved ? ' echip-id' : ''}`}>{label}</span>
    </>
  );

  const card = enabled && open
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

  if (resolved.navigable && resolved.route) {
    return (
      <>
        <Link ref={setReference} {...getReferenceProps({ className: cls.join(' '), to: projectPath(pid, resolved.route) })}>
          {inner}
        </Link>
        {card}
      </>
    );
  }

  return (
    <>
      <button ref={setReference} type="button" {...getReferenceProps({ className: cls.join(' '), onClick: (e) => e.preventDefault() })}>
        {inner}
      </button>
      {card}
    </>
  );
}
