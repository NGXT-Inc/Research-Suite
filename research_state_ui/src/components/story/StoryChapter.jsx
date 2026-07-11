import { forwardRef } from 'react';
import { fmtDayTime } from '../../utils/format';
import StoryBeat from './StoryBeat';

/**
 * StoryChapter — one act of the story. Collapsed it is a single
 * "previously on…" line: title, span, plain-words headline. Open, it
 * narrates beat by beat. The Now chapter carries the live work and, when a
 * reflection wave is moving, the "reflection in progress" notice.
 */

function spanLabel(chapter) {
  const from = fmtDayTime(chapter.startIso);
  if (chapter.isNow) {
    return from ? `since ${from.day}` : 'awaiting the next move';
  }
  const to = fmtDayTime(chapter.endIso);
  if (!from) return '';
  if (!to || to.day === from.day) return from.day;
  return `${from.day} – ${to.day}`;
}

const StoryChapter = forwardRef(function StoryChapter(
  { chapter, beats, hiddenCount, open, following, onToggle, onFollow },
  ref,
) {
  const span = spanLabel(chapter);
  return (
    <article
      ref={ref}
      className={`story-chapter${chapter.isNow ? ' story-chapter--now' : ''}`}
    >
      <header className="story-chapter-head">
        <button
          type="button"
          className="story-chapter-toggle"
          onClick={onToggle}
          aria-expanded={open}
          disabled={following}
        >
          <span className="story-chapter-chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
          <span className="story-chapter-title">
            {chapter.title}
            {chapter.isNow && chapter.tally.live > 0 && (
              <span className="story-live-dot" aria-hidden="true" />
            )}
          </span>
          {chapter.waveTitle && <span className="story-chapter-wavetitle">{chapter.waveTitle}</span>}
          {span && <span className="story-chapter-span">{span}</span>}
          <span className="story-chapter-headline">{chapter.headline}</span>
        </button>
      </header>

      {open && (
        <div className="story-chapter-body">
          {chapter.openWave && (
            <div className="story-wave-open">
              <span className="story-live-dot" aria-hidden="true" />
              Reflection in progress — the project is pausing to take stock.
              <button
                type="button"
                className="story-wave-link"
                onClick={() => document.getElementById('project-synthesis')?.scrollIntoView({ block: 'start' })}
              >
                View synthesis ↓
              </button>
            </div>
          )}
          {beats.length === 0 ? (
            <div className="story-chapter-quiet muted">Nothing on this thread here.</div>
          ) : (
            <ol className="story-beats">
              {beats.map(beat => (
                <StoryBeat key={beat.key} beat={beat} onFollow={onFollow} />
              ))}
            </ol>
          )}
          {hiddenCount > 0 && (
            <div className="story-chapter-quiet muted">
              {hiddenCount} quieter beat{hiddenCount === 1 ? '' : 's'} off this thread.
            </div>
          )}
        </div>
      )}
    </article>
  );
});

export default StoryChapter;
