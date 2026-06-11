import { useEffect, useState } from 'react';
import { api } from '../api';
import MarkdownView from './MarkdownView';
import FileRenderer from './FileRenderer';
import { formatBytes, isMarkdown } from '../utils/format';

/**
 * ReportSpotlight — the results artifact.
 *
 * Once an experiment has run, the results report (role `report`) is the face
 * of the executed experiment: what the human reads first and what the
 * experiment reviewer grades against the plan's Evaluation section. Same
 * treatment as PlanSpotlight: compact header bar (status + path + size +
 * toggle) above the rendered body.
 *
 * The body renders inline markdown — prose, GFM metrics tables, and figures:
 * relative image links (e.g. `figures/loss.png`) resolve through the resource
 * file endpoint's `rel` parameter, so PNGs saved next to the report display
 * inline.
 */
export default function ReportSpotlight({
  projectId,
  reportResource,
  experimentReviews,
  experimentStatus,
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showBody, setShowBody] = useState(true);

  useEffect(() => {
    if (!reportResource) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);
    api.getResourceContent(projectId, reportResource.id)
      .then(d => { if (!cancelled) setContent(d); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [projectId, reportResource?.id, reportResource?.version_token]);

  if (!reportResource) return null;

  const latestReview = (experimentReviews || [])[experimentReviews.length - 1];
  let reportStatus = 'drafting';
  if (latestReview?.verdict === 'pass') reportStatus = 'accepted';
  else if (experimentStatus === 'experiment_review') reportStatus = 'under review';
  else if ((experimentReviews || []).length > 0) reportStatus = 'revising';

  const size = reportResource.size_bytes ?? content?.size_bytes;

  return (
    <section id="report" className="spotlight">
      <header className="spotlight-head spotlight-head--row">
        <div className="spotlight-head-left">
          <span className="spotlight-eyebrow">Results report</span>
          <span className={`plan-status plan-status--${reportStatus.replace(/\s+/g, '_')}`}>{reportStatus}</span>
        </div>
        <div className="spotlight-head-right">
          <span className="mono spotlight-bar-path">{reportResource.path}</span>
          <span className="spotlight-bar-sep">·</span>
          <span className="spotlight-bar-meta">{formatBytes(size)}</span>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowBody(v => !v)}
          >
            {showBody ? 'Hide report' : 'Show report'}
          </button>
        </div>
      </header>

      {showBody && (
        <div className="spotlight-body">
          {loading ? (
            <div className="empty">Loading report…</div>
          ) : error ? (
            <div className="error-message">{error}</div>
          ) : content ? (
            content.is_binary ? (
              <div className="empty">Binary report file</div>
            ) : isMarkdown(reportResource.path) ? (
              <MarkdownView
                text={content.content ?? ''}
                resolveImageSrc={(src) => api.resourceFileUrl(projectId, reportResource.id, src)}
              />
            ) : (
              <FileRenderer text={content.content ?? ''} path={reportResource.path} />
            )
          ) : null}
        </div>
      )}
    </section>
  );
}
