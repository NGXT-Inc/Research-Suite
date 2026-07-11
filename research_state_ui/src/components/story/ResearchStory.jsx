import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  useProjectStore,
  selectClaims,
  selectExperiments,
  selectEventsAll,
} from '../../store/useProjectStore';
import { useReflections } from '../../store/useReflections';
import { fmtDayTime } from '../../utils/format';
import { buildStory, beatInThread } from './storyModel';
import { clearKinHover } from './kinHover';
import StoryRibbon from './StoryRibbon';
import StoryChapter from './StoryChapter';

/**
 * ResearchStory — the project's narrative arc, on Home.
 *
 * One derived view, three levels of disclosure:
 *   0. the ribbon — the whole project's shape in one strip (a dot per beat,
 *      an act per reflection wave), click to jump;
 *   1. chapters — past acts collapsed to a one-line "previously on…"
 *      headline, the live Now chapter open;
 *   2. beats — the sentences inside a chapter, each expandable in place to
 *      its evidence (intent, conclusion, reviewer's word, rationale).
 *
 * Threads: any beat that touches a claim can be followed — the story
 * refocuses to that claim's arc across every chapter, everything else
 * steps back. Hovering a beat softly lights up its kin (same claim)
 * elsewhere in the story.
 *
 * Everything is derived from the polled home snapshot + events window +
 * the shared reflections poll; the story updates live as the agent works.
 */
export default function ResearchStory() {
  const projectId = useProjectStore(s => s.projectId);
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEventsAll);
  const reflections = useReflections(projectId);
  const waves = reflections?.syntheses;

  const story = useMemo(
    () => buildStory({ claims, experiments, events, waves: waves || [] }),
    [claims, experiments, events, waves],
  );

  // Thread following — the claim whose arc the story is refocused on.
  const [followId, setFollowId] = useState(null);
  useEffect(() => { setFollowId(null); }, [projectId]);
  const followedClaim = useMemo(
    () => (followId ? claims.find(c => c.id === followId) || null : null),
    [followId, claims],
  );
  const follow = useCallback((claimId) => {
    setFollowId(prev => (prev === claimId ? null : claimId));
  }, []);
  const clearFollow = useCallback(() => setFollowId(null), []);

  // A hovered beat can unmount under the cursor when a poll re-derives the
  // story (mouseleave never fires on removal) — reset so kin highlights
  // can't stick.
  useEffect(() => clearKinHover(), [story]);

  // Chapter disclosure: a Set of chapter ids the user flipped AWAY from
  // their default (Now open, past collapsed). Stored intent stays relative,
  // so when a wave publishes and 'now' becomes a fresh chapter, a stale
  // absolute "closed" can't collapse the new live chapter.
  const [flipped, setFlipped] = useState(() => new Set());
  useEffect(() => { setFlipped(new Set()); }, [projectId]);
  const toggleChapter = useCallback((id) => {
    setFlipped(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const chapterRefs = useRef(new Map());
  const followRef = useRef(followId);
  followRef.current = followId;
  const jumpToChapter = useCallback((id) => {
    // While following a thread, disclosure is derived (thread presence), so
    // don't write flips the user will only discover after unfollowing.
    if (!followRef.current) {
      setFlipped(prev => {
        const defaultOpen = id === 'now';
        const isOpen = prev.has(id) ? !defaultOpen : defaultOpen;
        if (isOpen) return prev;
        const next = new Set(prev);
        if (defaultOpen) next.delete(id);
        else next.add(id);
        return next;
      });
    }
    // After the expand renders, bring the chapter into view.
    requestAnimationFrame(() => {
      const el = chapterRefs.current.get(id);
      if (!el) return;
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
      el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    });
  }, []);

  const chapters = useMemo(() => story.chapters.map((chapter) => {
    const threadBeats = followId
      ? chapter.beats.filter(b => beatInThread(b, followId))
      : chapter.beats;
    const defaultOpen = chapter.isNow;
    const open = followId
      ? threadBeats.length > 0
      : (flipped.has(chapter.id) ? !defaultOpen : defaultOpen);
    return { chapter, threadBeats, open };
  }), [story, followId, flipped]);

  if (story.beatCount === 0) {
    return (
      <section className="section story" aria-label="Research story">
        <div className="section-title">The story so far</div>
        <div className="empty-state empty-state--compact">
          <p>The story starts with the first claim or experiment.</p>
        </div>
      </section>
    );
  }

  // Shifts older than the polled event window exist in the record but not
  // here — say so rather than narrating early chapters as quieter than they
  // were. Only when some chapter actually starts before the horizon.
  const horizon = story.shiftHorizonIso;
  const historyTruncated = Boolean(
    horizon && story.chapters.some(c => c.startIso && c.startIso < horizon),
  );

  return (
    <section className="section story" aria-label="Research story">
      <div className="cluster--between" style={{ marginBottom: 10 }}>
        <div className="section-title" style={{ marginBottom: 0 }}>The story so far</div>
        <span className="story-count muted">
          {story.beatCount} beat{story.beatCount === 1 ? '' : 's'} · {story.chapters.length} chapter{story.chapters.length === 1 ? '' : 's'}
        </span>
      </div>

      {story.chapters.length > 1 && (
        <StoryRibbon
          chapters={story.chapters}
          followId={followId}
          onJump={jumpToChapter}
        />
      )}

      {followedClaim && (
        <div className="story-follow" role="status">
          <span className="story-follow-label">Following a thread</span>
          <span className="story-follow-statement">{followedClaim.statement}</span>
          <button type="button" className="story-follow-clear" onClick={clearFollow} aria-label="Stop following this thread">
            ✕
          </button>
        </div>
      )}

      <div className="story-chapters">
        {chapters.map(({ chapter, threadBeats, open }) => (
          <StoryChapter
            key={chapter.id}
            ref={(el) => {
              if (el) chapterRefs.current.set(chapter.id, el);
              else chapterRefs.current.delete(chapter.id);
            }}
            chapter={chapter}
            beats={threadBeats}
            hiddenCount={chapter.beats.length - threadBeats.length}
            open={open}
            following={Boolean(followId)}
            onToggle={() => toggleChapter(chapter.id)}
            onFollow={follow}
          />
        ))}
      </div>

      {historyTruncated && (
        <div className="story-horizon muted">
          Belief shifts before {fmtDayTime(horizon)?.day || 'the event window'} are in the record but not shown here.
        </div>
      )}
    </section>
  );
}
