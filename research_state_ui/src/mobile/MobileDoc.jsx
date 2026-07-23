import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import PlanBody from '../components/PlanBody';
import MarkdownView from '../components/MarkdownView';
import FileRenderer from '../components/FileRenderer';
import ReviewEvolutionStepper from '../components/ReviewEvolutionStepper';
import ExperimentReviewStepper from '../components/ExperimentReviewStepper';
import { isMarkdown } from '../utils/format';

/**
 * MobileDoc — a plan or report artifact as pure content. The document is the
 * section, so the desktop spotlight chrome — file path, status pill,
 * show/hide toggles — doesn't exist here. One quiet disclosure row above
 * the body carries the review trail; everything else is the document.
 */
export default function MobileDoc({
  projectId,
  artifact,
  reviews = [],
  kind, // 'plan' | 'report'
  experimentStatus,
  attemptIndex,
}) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState(null);
  const [showReview, setShowReview] = useState(false);

  useEffect(() => {
    if (!artifact) return undefined;
    let cancelled = false;
    setContent(null);
    setError(null);
    api.getArtifactContent(projectId, artifact.id)
      .then(d => { if (!cancelled) setContent(d); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [projectId, artifact?.id]);

  // Stable identity: MarkdownView keys its `img` component (and its memo) on
  // this — an inline arrow here would remount every figure per re-render.
  const artifactId = artifact?.id;
  const resolveImageSrc = useCallback(
    (src) => api.artifactFigureUrl(projectId, artifactId, src),
    [projectId, artifactId],
  );

  if (!artifact) return null;

  const inReview = experimentStatus === (kind === 'plan' ? 'design_review' : 'experiment_review');
  const latest = reviews[reviews.length - 1];
  let verdict = 'drafting';
  if (latest?.verdict === 'pass') verdict = 'accepted';
  else if (inReview) verdict = 'under review';
  else if (reviews.length > 0) verdict = 'revising';

  return (
    <div className="mdoc">
      {(reviews.length > 0 || inReview) && (
        showReview ? (
          <div className="mdoc-review">
            {kind === 'plan' ? (
              <ReviewEvolutionStepper
                reviews={reviews}
                currentAttempt={attemptIndex}
                experimentStatus={experimentStatus}
              />
            ) : reviews.length > 0 ? (
              <ExperimentReviewStepper reviews={reviews} />
            ) : (
              <div className="mquiet">awaiting reviewer</div>
            )}
          </div>
        ) : (
          <button type="button" className="mterm-row" onClick={() => setShowReview(true)}>
            <span className="mterm-twist" aria-hidden="true">▸</span>
            review · {verdict}{reviews.length > 1 ? ` · v${reviews.length}` : ''}
          </button>
        )
      )}

      {error ? (
        <div className="error-message">{error}</div>
      ) : !content ? (
        <div className="mquiet">loading…</div>
      ) : content.is_binary ? (
        <div className="mquiet">binary file</div>
      ) : kind === 'plan' ? (
        <PlanBody text={content.content ?? ''} path={artifact.path} resolveImageSrc={resolveImageSrc} />
      ) : isMarkdown(artifact.path) ? (
        <MarkdownView text={content.content ?? ''} resolveImageSrc={resolveImageSrc} />
      ) : (
        <FileRenderer text={content.content ?? ''} path={artifact.path} />
      )}
    </div>
  );
}
