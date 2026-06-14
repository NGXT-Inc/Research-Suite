import { useEffect, useState } from 'react';
import { api } from '../api';
import FileRenderer from './FileRenderer';
import PdfView from './PdfView';
import SourceBadge from './SourceBadge';
import ContentUnavailable from './ContentUnavailable';
import { formatBytes, isMarkdown } from '../utils/format';

// Drop a leading "# <title>" from markdown when it just repeats a name already
// shown elsewhere (the panel header). Only the very first heading, and only on
// an exact (trimmed, case-insensitive) match — otherwise the file keeps its own
// title untouched.
function stripMatchingH1(md, title) {
  if (!md || !title) return md;
  const m = md.match(/^\s*#\s+(.+?)\s*#*\s*(?:\r?\n|$)/);
  if (m && m[1].trim().toLowerCase() === String(title).trim().toLowerCase()) {
    return md.slice(m[0].length).replace(/^\s+/, '');
  }
  return md;
}

function extOf(path) {
  if (!path) return '';
  const name = path.split('/').pop() || '';
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i + 1).toLowerCase();
}

function isPdfPath(path) {
  return extOf(path) === 'pdf';
}

// Raster images render inline straight from the file endpoint (like PDFs) —
// no /content fetch. SVG is deliberately absent: it is text, so it flows
// through FileRenderer → SvgView (which also offers a source toggle).
const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'ico', 'tiff', 'avif',
]);
function isImagePath(path) {
  return IMAGE_EXTS.has(extOf(path));
}

// Extensions we never try to render as text. Some backends return the raw
// bytes of these as a (huge) decoded string with no is_binary flag — e.g. a
// 29 MB .pt checkpoint comes back as ~27 MB of "text" — which locks up the
// renderer. Short-circuit by extension (like PDFs) so we never fetch or render
// them inline; the raw-file link is the escape hatch.
const BINARY_EXTS = new Set([
  'pt', 'pth', 'ckpt', 'safetensors', 'bin', 'pkl', 'pickle', 'npy', 'npz',
  'onnx', 'h5', 'hdf5', 'pb', 'model', 'weights', 'joblib',
  'zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar',
  'so', 'o', 'a', 'dylib', 'dll', 'exe', 'wasm', 'class', 'jar',
  'parquet', 'feather', 'arrow', 'db', 'sqlite', 'sqlite3',
  'wav', 'mp3', 'flac', 'ogg', 'mp4', 'mov', 'avi', 'mkv', 'webm',
]);
function isBinaryPath(path) {
  return BINARY_EXTS.has(extOf(path));
}

// Hard cap on how much text we hand to the renderers. A genuine text file can
// still be enormous (a 15 MB results CSV); slicing before render keeps the tab
// responsive. The raw-file link serves the untruncated bytes.
const MAX_PREVIEW_CHARS = 200_000;

export default function ResourceContentView({
  projectId, resourceId, size, path,
  // Panel-context trims: hide the provenance badge entirely, and drop a
  // leading H1 that just echoes the title shown in the panel header.
  hideSource = false, dedupeTitle = null,
}) {
  // PDFs render directly via the file endpoint (the browser's PDF viewer
  // streams the bytes). Known-binary types (model weights, archives, media)
  // never render inline at all. Both skip /content — for PDFs it would just
  // return is_binary:true with no payload; for binaries it could return many
  // MB of useless decoded text (see isBinaryPath).
  const renderPdf = isPdfPath(path);
  const renderImage = !renderPdf && isImagePath(path);
  const renderBinary = !renderPdf && !renderImage && isBinaryPath(path);

  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Skip the /content fetch entirely for PDFs, images, and known-binary
    // files — the iframe / <img> / raw link handle them. Important: this
    // effect still runs (hooks must keep the same order across renders), it
    // just no-ops.
    if (renderPdf || renderImage || renderBinary) {
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
  }, [projectId, resourceId, renderPdf, renderImage, renderBinary]);

  if (renderPdf) {
    return (
      <div>
        <PdfView projectId={projectId} resourceId={resourceId} path={path} />
      </div>
    );
  }

  if (renderImage) {
    return (
      <div className="image-view">
        {/* Not lazy: inside a small overflow:auto preview panel a lazy image
            sits "below the fold" of the scroll box and never loads. */}
        <img
          className="image-view-img"
          src={api.resourceFileUrl(projectId, resourceId)}
          alt={path || 'image'}
        />
      </div>
    );
  }

  if (renderBinary) {
    return (
      <div className="empty">
        Binary file{size != null ? ` (${formatBytes(size)})` : ''}.{' '}
        <a className="btn btn--sm" href={api.resourceFileUrl(projectId, resourceId)} target="_blank" rel="noreferrer">Open raw</a>
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

  if (content.available === false) {
    return (
      <ContentUnavailable
        content={content}
        fallbackLink={{ href: api.resourceFileUrl(projectId, resourceId), label: 'Open raw file' }}
      />
    );
  }

  const isBinary = content.is_binary || !('content' in content);
  const fullText = content.content ?? '';
  const overCap = fullText.length > MAX_PREVIEW_CHARS;
  let text = overCap ? fullText.slice(0, MAX_PREVIEW_CHARS) : fullText;
  if (dedupeTitle && isMarkdown(path || content.path)) {
    text = stripMatchingH1(text, dedupeTitle);
  }
  const meta = content.size_bytes ?? size;

  return (
    <div>
      {!hideSource && <SourceBadge source={content.source} versionId={content.version_id} />}
      {(content.truncated || overCap) && (
        <div className="content-truncated-note">File is large — preview is truncated.</div>
      )}
      {isBinary ? (
        <div className="empty">
          Binary file ({formatBytes(meta)}). <a className="btn btn--sm" href={api.resourceFileUrl(projectId, resourceId)} target="_blank" rel="noreferrer">Open raw</a>
        </div>
      ) : (
        <FileRenderer
          text={text}
          path={path || content.path}
          resolveImageSrc={(src) => api.resourceFileUrl(projectId, resourceId, src)}
        />
      )}
    </div>
  );
}
