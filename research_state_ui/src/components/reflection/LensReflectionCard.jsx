import { useState } from 'react';
import ArtifactContentView from '../ArtifactContentView';

/**
 * LensReflectionCard — one roster lens and the reflection its subagent wrote.
 *
 * Collapsed, it's a compact card in the roster grid (just the lens name + its
 * angle). Clicking anywhere on a covered card — or its top-right expand icon —
 * opens it to the full roster width (it spans every column and pushes the rest
 * down) so the reflection markdown reads at a comfortable measure. The markdown
 * is lazy-mounted on open, so a five-lens wave doesn't fire five fetches at
 * once; each wave renders the exact artifact it submitted (faithful history).
 */
export default function LensReflectionCard({ projectId, lens, reflection }) {
  const [open, setOpen] = useState(false);
  const covered = Boolean(reflection?.covered && reflection?.artifactId);
  const toggle = () => { if (covered) setOpen(v => !v); };

  return (
    <div
      className={
        'refl-lens-card'
        + (covered ? ' refl-lens-card--clickable' : '')
        + (open ? ' refl-lens-card--open' : '')
      }
      onClick={covered ? toggle : undefined}
    >
      <div className="refl-lens-card-head">
        <span className={`refl-lens-title${lens.core ? ' refl-lens-title--core' : ''}`}>
          {lens.title || lens.id}
        </span>
        {covered && (
          <button
            type="button"
            className="refl-lens-expand"
            onClick={(e) => { e.stopPropagation(); toggle(); }}
            aria-expanded={open}
            aria-label={open ? 'Collapse reflection' : 'Expand reflection'}
            title={open ? 'Collapse' : 'Expand'}
          >
            {open ? <CollapseIcon /> : <ExpandIcon />}
          </button>
        )}
      </div>

      {lens.charter && <div className="refl-lens-charter">{lens.charter}</div>}

      {!covered && <div className="refl-lens-pending">reflection not submitted yet</div>}

      {open && covered && (
        <div className="refl-lens-body" onClick={(e) => e.stopPropagation()}>
          <section className="spotlight refl-lens-doc">
            <header className="spotlight-head spotlight-head--row">
              <div className="spotlight-head-left">
                <span className="spotlight-eyebrow">
                  {lens.title || lens.id} reflection
                </span>
              </div>
              <div className="spotlight-head-right">
                <span className="mono spotlight-bar-path" title={reflection.path}>
                  {reflection.path}
                </span>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => setOpen(false)}
                >
                  Hide reflection
                </button>
              </div>
            </header>
            <div className="spotlight-body">
              <ArtifactContentView
                projectId={projectId}
                artifactId={reflection.artifactId}
                path={reflection.path}
              />
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function ExpandIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

function CollapseIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3" />
    </svg>
  );
}
