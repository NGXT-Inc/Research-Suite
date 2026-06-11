import { useEffect, useState } from 'react';
import { api } from '../api';
import FileRenderer from './FileRenderer';
import PdfView from './PdfView';
import { formatBytes } from '../utils/format';

function isPdfPath(path) {
  if (!path) return false;
  const name = path.split('/').pop() || '';
  return name.toLowerCase().endsWith('.pdf');
}

export default function ResourceContentView({ projectId, resourceId, size, path }) {
  // PDFs are rendered directly via the file endpoint (the browser's PDF
  // viewer streams the bytes). We skip /content entirely — calling it would
  // just return is_binary:true with no payload, and the iframe handles
  // loading state itself.
  const renderPdf = isPdfPath(path);

  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Skip the /content fetch entirely for PDFs — the iframe handles
    // loading. Important: this effect still runs (hooks must keep the same
    // order across renders), it just no-ops.
    if (renderPdf) {
      setContent(null);
      setLoading(false);
      setError(null);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);
    api.getResourceContent(projectId, resourceId)
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
  }, [projectId, resourceId, renderPdf]);

  if (renderPdf) {
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
          Binary file ({formatBytes(meta)}). <a className="btn btn--sm" href={api.resourceFileUrl(projectId, resourceId)} target="_blank" rel="noreferrer">Open raw</a>
        </div>
      ) : (
        <FileRenderer text={text} path={path || content.path} />
      )}
    </div>
  );
}
