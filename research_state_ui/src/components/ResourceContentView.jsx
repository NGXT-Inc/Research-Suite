import { useEffect, useState } from 'react';
import { api } from '../api';
import FileRenderer from './FileRenderer';
import PdfView from './PdfView';

function humanBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function isPdfPath(path) {
  if (!path) return false;
  const name = path.split('/').pop() || '';
  return name.toLowerCase().endsWith('.pdf');
}

export default function ResourceContentView({ projectId, resourceId, size, path, versionId = null }) {
  // Live PDFs are rendered directly via the file endpoint (the browser's PDF
  // viewer streams the bytes). We skip /content entirely — calling it would
  // just return is_binary:true with no payload, and the iframe handles
  // loading state itself. Historical PDF versions still go through the
  // /content path below; they'll show "metadata_only" since shadow-git
  // doesn't store binary blobs.
  const renderLivePdf = isPdfPath(path) && !versionId;

  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Skip the /content fetch entirely for live PDFs — the iframe handles
    // loading. Important: this effect still runs (hooks must keep the same
    // order across renders), it just no-ops.
    if (renderLivePdf) {
      setContent(null);
      setLoading(false);
      setError(null);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);
    const fetcher = versionId
      ? api.getResourceVersionContent(projectId, resourceId, versionId)
      : api.getResourceContent(projectId, resourceId);
    fetcher
      .then(data => {
        if (cancelled) return;
        setContent(data);
      })
      .catch(err => {
        if (cancelled) return;
        setError(err.message);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [projectId, resourceId, versionId, renderLivePdf]);

  if (renderLivePdf) {
    return (
      <div>
        <PdfView projectId={projectId} resourceId={resourceId} path={path} />
      </div>
    );
  }

  if (loading) return <div className="empty">Loading…</div>;
  if (error) {
    return (
      <div>
        <div className="error-message">{error}</div>
        <a className="btn btn--sm" href={api.resourceFileUrl(projectId, resourceId)} target="_blank" rel="noreferrer">
          Open raw file
        </a>
      </div>
    );
  }
  if (!content) return null;

  // Version endpoint returns a different shape: { available, text, version, reason }
  // Live endpoint returns: { content, is_binary, size_bytes }
  if (versionId) {
    if (content.available === false) {
      return (
        <div className="version-unavailable">
          <div className="version-unavailable-title">
            {content.reason === 'metadata_only'
              ? 'Historical content not stored for large or binary files'
              : 'Snapshot unavailable; metadata remains'}
          </div>
          <div className="version-unavailable-sub">
            Showing metadata only for this version.
          </div>
        </div>
      );
    }
    return (
      <div>
        <FileRenderer text={content.text ?? ''} path={path || content.version?.path} />
      </div>
    );
  }

  const isBinary = content.is_binary || !('content' in content);
  const text = content.content ?? '';
  const meta = content.size_bytes ?? size;

  return (
    <div>
      {content.truncated && (
        <div className="content-truncated-note">File is large — preview is truncated.</div>
      )}
      {isBinary ? (
        <div className="empty">
          Binary file ({humanBytes(meta)}). <a className="btn btn--sm" href={api.resourceFileUrl(projectId, resourceId)} target="_blank" rel="noreferrer">Open raw</a>
        </div>
      ) : (
        <FileRenderer text={text} path={path || content.path} />
      )}
    </div>
  );
}
