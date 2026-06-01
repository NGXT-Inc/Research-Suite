import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import FileRenderer from './FileRenderer';
import ReviewEvolutionStepper from './ReviewEvolutionStepper';

function bytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

/**
 * Build an attempt → version_id map for this experiment's plan resource from
 * the resource's `associations` array. Each association includes
 * {target_type, target_id, role, attempt_index, version_id}. Missing or null
 * version_ids mean no historical snapshot is available for that attempt
 * (legacy data, or the file wasn't observed at that attempt).
 */
function buildAttemptVersionMap(planResource, experimentId) {
  const map = new Map();
  const assocs = Array.isArray(planResource?.associations) ? planResource.associations : [];
  for (const a of assocs) {
    if (a.target_type === 'experiment' && a.target_id === experimentId && a.role === 'plan') {
      map.set(a.attempt_index, a.version_id || null);
    }
  }
  return map;
}

/**
 * PlanSpotlight — the design artifact.
 *
 * The plan resource is the framing document of the experiment, so we render
 * its content inline. Above the body, a compact header bar (path + version
 * + size + status), and the ReviewEvolutionStepper showing v1→v2→…→accepted.
 *
 * Version-aware: clicking a v_k pill in the stepper renders the plan at
 * the version that was associated with that attempt. When viewing a past
 * version (or when the live file has advanced beyond the accepted version),
 * the header shows a "v_k of N — live file has advanced" indicator.
 */
export default function PlanSpotlight({
  projectId,
  experimentId,
  planResource,
  designReviews,
  attemptIndex,
  experimentStatus,
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showBody, setShowBody] = useState(true);
  const [selectedAttempt, setSelectedAttempt] = useState(attemptIndex);

  // Reset to the current attempt whenever it advances (new attempt landed).
  useEffect(() => { setSelectedAttempt(attemptIndex); }, [attemptIndex]);

  const versionMap = useMemo(
    () => buildAttemptVersionMap(planResource, experimentId),
    [planResource, experimentId],
  );

  // Which version_id are we trying to render right now? The selected attempt
  // maps to a specific version (or null = no snapshot recorded).
  const selectedVersionId = versionMap.get(selectedAttempt) ?? null;
  const currentVersionId = planResource?.current_version_id ?? null;
  const onCurrentAttempt = selectedAttempt === attemptIndex;
  const liveFileAdvanced = !!(currentVersionId && selectedVersionId && selectedVersionId !== currentVersionId);

  // Build the availability map for the stepper so we can dim version pills
  // whose snapshot wasn't captured. Always treat the current attempt as
  // available (we can always render the live file).
  const versionAvailability = useMemo(() => {
    const m = {};
    for (let v = 1; v <= attemptIndex; v++) {
      const vid = versionMap.get(v);
      m[v] = !!vid || v === attemptIndex;
    }
    return m;
  }, [versionMap, attemptIndex]);

  // Content fetching strategy:
  //   - viewing current attempt: prefer live content endpoint (fastest path,
  //     handles legacy resources with null current_version)
  //   - viewing past attempt with a version_id: hit /versions/{vid}/content
  //   - viewing past attempt without a version_id: render an "unavailable"
  //     state without fetching
  useEffect(() => {
    if (!planResource) return undefined;
    let cancelled = false;

    if (onCurrentAttempt) {
      setLoading(true);
      setError(null);
      setContent(null);
      api.getResourceContent(projectId, planResource.id)
        .then(d => { if (!cancelled) setContent({ kind: 'live', data: d }); })
        .catch(e => { if (!cancelled) setError(e.message); })
        .finally(() => !cancelled && setLoading(false));
      return () => { cancelled = true; };
    }

    if (!selectedVersionId) {
      // No snapshot for this past attempt — render a placeholder, no fetch.
      setContent({ kind: 'unavailable', reason: 'no_snapshot' });
      setError(null);
      setLoading(false);
      return undefined;
    }

    setLoading(true);
    setError(null);
    setContent(null);
    api.getResourceVersionContent(projectId, planResource.id, selectedVersionId)
      .then(d => { if (!cancelled) setContent({ kind: 'version', data: d }); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [projectId, planResource?.id, selectedAttempt, selectedVersionId, onCurrentAttempt]);

  if (!planResource) {
    return (
      <section id="design" className="spotlight">
        <div className="spotlight-eyebrow">Plan</div>
        <div className="spotlight-empty">
          No plan resource registered for this attempt yet.
        </div>
      </section>
    );
  }

  const latestReview = designReviews[designReviews.length - 1];
  let planStatus = 'drafting';
  if (latestReview?.verdict === 'pass') planStatus = 'accepted';
  else if (experimentStatus === 'design_review') planStatus = 'under review';
  else if (designReviews.length > 0) planStatus = 'revising';

  const size = onCurrentAttempt
    ? (planResource.size_bytes ?? content?.data?.size_bytes)
    : content?.data?.version?.size_bytes;

  return (
    <section id="design" className="spotlight">
      <header className="spotlight-head spotlight-head--row">
        <div className="spotlight-head-left">
          <span className="spotlight-eyebrow">Plan</span>
          <span className={`plan-status plan-status--${planStatus.replace(/\s+/g, '_')}`}>{planStatus}</span>
          {!onCurrentAttempt && (
            <span className="plan-version-tag" title={`Viewing plan as it was at attempt ${selectedAttempt}`}>
              v{selectedAttempt} of {attemptIndex}
            </span>
          )}
          {liveFileAdvanced && (
            <button
              type="button"
              className="plan-version-advanced"
              onClick={() => setSelectedAttempt(attemptIndex)}
              title="The live plan file has changed since this version. Click to return to the current version."
            >
              live file has advanced
            </button>
          )}
        </div>
        <div className="spotlight-head-right">
          <span className="mono spotlight-bar-path">{planResource.path}</span>
          <span className="spotlight-bar-sep">·</span>
          <span className="spotlight-bar-meta">v{selectedAttempt}</span>
          <span className="spotlight-bar-sep">·</span>
          <span className="spotlight-bar-meta">{bytes(size)}</span>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowBody(v => !v)}
          >
            {showBody ? 'Hide plan' : 'Show plan'}
          </button>
        </div>
      </header>

      {showBody && (
        <div className="spotlight-body">
          {loading ? (
            <div className="empty">Loading plan…</div>
          ) : error ? (
            <div className="error-message">{error}</div>
          ) : content?.kind === 'unavailable' ? (
            <div className="version-unavailable">
              <div className="version-unavailable-title">Historical snapshot not available</div>
              <div className="version-unavailable-sub">
                v{selectedAttempt} of this plan wasn't captured by the backend (this attempt predates
                resource versioning, or the file wasn't observed at that attempt).
              </div>
            </div>
          ) : content?.kind === 'live' ? (
            content.data.is_binary ? (
              <div className="empty">Binary plan file</div>
            ) : (
              <FileRenderer text={content.data.content ?? ''} path={planResource.path} />
            )
          ) : content?.kind === 'version' ? (
            content.data.available === false ? (
              <div className="version-unavailable">
                <div className="version-unavailable-title">
                  {content.data.reason === 'metadata_only'
                    ? 'Historical content not stored for large or binary files'
                    : 'Snapshot unavailable; metadata remains'}
                </div>
                <div className="version-unavailable-sub">
                  Showing metadata only for v{selectedAttempt}. The full content of this version is no longer renderable here.
                </div>
              </div>
            ) : (
              <FileRenderer
                text={content.data.text ?? ''}
                path={planResource.path}
              />
            )
          ) : null}
        </div>
      )}

      {(designReviews.length > 0 || experimentStatus === 'design_review') && (
        <div className="plan-history">
          <div className="plan-history-head">Review history</div>
          <ReviewEvolutionStepper
            reviews={designReviews}
            currentAttempt={attemptIndex}
            experimentStatus={experimentStatus}
            selectedAttempt={selectedAttempt}
            onSelectVersion={(v) => setSelectedAttempt(v)}
            versionAvailability={versionAvailability}
          />
        </div>
      )}
    </section>
  );
}
