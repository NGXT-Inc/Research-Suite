/**
 * SourceMeta — muted, inline provenance for a spotlight titlebar; the low-key
 * sibling of SourceBadge (the pill). A submitted artifact shows its pinned
 * version as quiet metadata (alongside path · size) so a human knows the body
 * is a snapshot — without a colored chip on its own line. The unremarkable
 * "live" case renders nothing.
 */
export default function SourceMeta({ source, versionId }) {
  if (source !== 'submitted' || !versionId) return null;
  return (
    <>
      <span className="spotlight-bar-sep">·</span>
      <span className="spotlight-bar-meta" title={versionId}>
        submitted v{versionId.slice(-6)}
      </span>
    </>
  );
}
