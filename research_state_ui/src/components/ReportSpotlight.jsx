import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import MarkdownView from './MarkdownView';
import FileRenderer from './FileRenderer';
import ExperimentReviewStepper from './ExperimentReviewStepper';
import { isMarkdown } from '../utils/format';

/**
 * ReportSpotlight — the results artifact.
 *
 * Once an experiment has run, the results report (role `report`) is the face
 * of the executed experiment: what the human reads first and what the
 * experiment reviewer grades against the plan's Evaluation section. Same
 * treatment as PlanSpotlight: compact header bar (status + path + size +
 * toggle) above the rendered body.
 *
 * The body renders inline markdown — prose and GFM metrics tables. Relative
 * image links resolve through the artifact figure endpoint, so figures
 * submitted with the report display inline (the backend serves them from the
 * blob store); a link never submitted falls back to a "figure not available"
 * placeholder.
 */
export default function ReportSpotlight({
  projectId,
  reportArtifact,
  experimentReviews,
  experimentStatus,
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showBody, setShowBody] = useState(true);
  const [showReview, setShowReview] = useState(false);

  useEffect(() => {
    if (!reportArtifact) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);
    api.getArtifactContent(projectId, reportArtifact.id)
      .then(d => { if (!cancelled) setContent(d); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [projectId, reportArtifact?.id]);

  // Stable identity: MarkdownView keys its `img` component (and its memo) on
  // this — an inline arrow here would remount every figure per re-render.
  const reportId = reportArtifact?.id;
  const resolveImageSrc = useCallback(
    (src) => api.artifactFigureUrl(projectId, reportId, src),
    [projectId, reportId],
  );

  if (!reportArtifact) return null;

  const latestReview = (experimentReviews || [])[experimentReviews.length - 1];
  let reportStatus = 'drafting';
  if (latestReview?.verdict === 'pass') reportStatus = 'accepted';
  else if (experimentStatus === 'experiment_review') reportStatus = 'under review';
  else if ((experimentReviews || []).length > 0) reportStatus = 'revising';

  const reviews = experimentReviews || [];
  const reviewAvailable = reviews.length > 0 || experimentStatus === 'experiment_review';

  return (
    <section id="report" className="spotlight">
      <header className="spotlight-head spotlight-head--row">
        <div className="spotlight-head-left">
          <span className="spotlight-eyebrow">Results report</span>
          <span className={`plan-status plan-status--${reportStatus.replace(/\s+/g, '_')}`}>{reportStatus}</span>
        </div>
        <div className="spotlight-head-right">
          <span className="mono spotlight-bar-path">{reportArtifact.path}</span>
          {reviewAvailable && (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setShowReview(v => !v)}
            >
              <span className="toggle-verb">{showReview ? 'Hide' : 'Show'}</span>{' review'}
            </button>
          )}
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => setShowBody(v => !v)}
          >
            <span className="toggle-verb">{showBody ? 'Hide' : 'Show'}</span>{' report'}
          </button>
        </div>
      </header>

      {showReview && reviewAvailable && (
        <div className="spotlight-review">
          {reviews.length > 0 ? (
            <ExperimentReviewStepper reviews={reviews} />
          ) : (
            <div className="empty" style={{ fontSize: 'var(--text-sm)' }}>Awaiting reviewer.</div>
          )}
        </div>
      )}

      {showBody && (
        <div className="spotlight-body">
          {loading ? (
            <div className="empty">Loading report…</div>
          ) : error ? (
            <div className="error-message">{error}</div>
          ) : content ? (
            content.is_binary ? (
              <div className="empty">Binary report file</div>
            ) : isMarkdown(reportArtifact.path) ? (
              <MarkdownView
                text={content.content ?? ''}
                resolveImageSrc={resolveImageSrc}
              />
            ) : (
              <FileRenderer text={content.content ?? ''} path={reportArtifact.path} />
            )
          ) : null}
        </div>
      )}
    </section>
  );
}
