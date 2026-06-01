/**
 * PdfView — inline PDF rendering via the browser's native viewer.
 *
 * The backend already serves `/api/projects/{pid}/resources/{rid}/file` with
 * `Content-Type: application/pdf` and `Content-Disposition: inline`, so every
 * modern browser (Chrome, Safari, Firefox, Edge) will mount its built-in PDF
 * viewer inside the iframe — which gives us page nav, search, zoom, print,
 * and download for free, without bundling pdf.js.
 *
 * URL fragment params (PDF Open Parameters):
 *   toolbar=0   — hide Chromium's PDF toolbar (the viewer's own header).
 *   navpanes=0  — hide the sidebar / thumbnail pane.
 *   view=FitH   — fit the page to the iframe width.
 *
 * Firefox / Safari ignore these silently. Ctrl/Cmd+F still searches inside
 * the PDF, Ctrl/Cmd +/- still zooms, right-click still offers Save/Print —
 * we're hiding the toolbar UI, not the underlying functionality.
 *
 * If we ever need programmatic access to the PDF (highlight a citation, jump
 * to a specific page from a claim, extract text) we can swap this for
 * react-pdf / pdfjs-dist. The component contract stays the same.
 */
import { api } from '../api';

function filenameOf(path) {
  if (!path) return 'document.pdf';
  return path.split('/').pop() || 'document.pdf';
}

export default function PdfView({ projectId, resourceId, path }) {
  const url = api.resourceFileUrl(projectId, resourceId);
  return (
    <iframe
      src={`${url}#toolbar=0&navpanes=0&view=FitH`}
      title={filenameOf(path)}
      className="pdf-view-frame"
      loading="lazy"
    />
  );
}
