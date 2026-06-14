import { useState } from 'react';
import CodeBlock from './CodeBlock';

/**
 * Render SVG source as an image.
 *
 * Loading the markup through an <img> data URL sandboxes it — scripts and
 * external fetches inside the SVG don't execute — so it's safe for arbitrary
 * repo SVGs. The canvas keeps a neutral light background because many figures
 * draw dark strokes on a transparent ground, which would vanish on the dark
 * app surface. A toggle flips to the raw markup through the shared CodeBlock.
 *
 * Reusable wherever a text resource is known to be SVG (FileRenderer dispatches
 * here; ResourceContentView / ReportSpotlight / PlanBody reach it through that).
 */
export default function SvgView({ text }) {
  const [showSource, setShowSource] = useState(false);
  const markup = text || '';
  // `;charset=utf-8,` (not the non-standard `;utf8,`, which <img> rejects).
  // encodeURIComponent emits %-encoded UTF-8, so non-ASCII markup is safe.
  const src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;

  return (
    <div className="svg-view">
      <div className="svg-view-bar">
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => setShowSource(s => !s)}
        >
          {showSource ? 'View image' : 'View source'}
        </button>
      </div>
      {showSource ? (
        <CodeBlock code={markup} language="markup" showLineNumbers={false} />
      ) : (
        <div className="svg-view-canvas">
          {/* Not lazy: inside a small overflow:auto preview panel a lazy image
              sits "below the fold" of the scroll box and never loads. */}
          <img className="svg-view-img" src={src} alt="SVG figure" />
        </div>
      )}
    </div>
  );
}
