import { outcomeColor, claimStatusColor } from '../../utils/evidence';
import { useKinHover } from './kinHover';
import { BEAT, beatInThread } from './storyModel';

/**
 * StoryRibbon — the whole project in one strip. One dot cluster per
 * chapter, one dot per beat (colored by what happened). Clicking a cluster
 * expands + scrolls to that chapter. While a thread is followed (or a beat
 * hovered), dots off the thread step back.
 */

const MAX_DOTS = 22;

function beatColor(beat) {
  switch (beat.type) {
    case BEAT.VERDICT: return outcomeColor(beat.outcome);
    case BEAT.LIVE: return 'var(--active)';
    case BEAT.SHIFT: return claimStatusColor(beat.shift.status ? beat.shift.status.to : null);
    case BEAT.STAKED: return 'var(--steel)';
    case BEAT.WAVE: return 'var(--mcp)';
    default: return 'var(--faint)';
  }
}

function dotsFor(chapter) {
  const beats = chapter.beats;
  if (beats.length <= MAX_DOTS) return { shown: beats, hidden: 0 };
  // Keep the tail (most recent) — the front condenses into a count.
  return { shown: beats.slice(beats.length - MAX_DOTS), hidden: beats.length - MAX_DOTS };
}

export default function StoryRibbon({ chapters, followId, onJump }) {
  const hoverIds = useKinHover(s => s.ids);
  const litIds = hoverIds && hoverIds.length > 0 ? hoverIds : null;
  return (
    <div className="story-ribbon" role="navigation" aria-label="Story chapters">
      {chapters.map((chapter) => {
        const { shown, hidden } = dotsFor(chapter);
        const live = chapter.isNow && chapter.tally.live > 0;
        return (
          <button
            key={chapter.id}
            type="button"
            className={`story-ribbon-seg${chapter.isNow ? ' story-ribbon-seg--now' : ''}`}
            onClick={() => onJump(chapter.id)}
            title={`${chapter.title} — ${chapter.headline}`}
          >
            <span className="story-ribbon-name">
              {chapter.isNow ? 'Now' : chapter.index}
              {live && <span className="story-live-dot" aria-hidden="true" />}
            </span>
            <span className="story-ribbon-dots" aria-hidden="true">
              {hidden > 0 && <span className="story-ribbon-more">+{hidden}</span>}
              {shown.map((beat) => {
                const inThread = followId ? beatInThread(beat, followId) : true;
                const kin = litIds ? litIds.some(id => beat.claimIds.includes(id)) : false;
                return (
                  <span
                    key={beat.key}
                    className={[
                      'story-ribbon-dot',
                      !inThread ? 'story-ribbon-dot--off' : '',
                      kin ? 'story-ribbon-dot--kin' : '',
                      beat.type === BEAT.LIVE ? 'story-ribbon-dot--live' : '',
                      beat.type === BEAT.WAVE ? 'story-ribbon-dot--wave' : '',
                    ].filter(Boolean).join(' ')}
                    style={{ background: beatColor(beat) }}
                  />
                );
              })}
            </span>
          </button>
        );
      })}
    </div>
  );
}
