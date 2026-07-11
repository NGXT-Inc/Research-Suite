import { memo, useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectHref } from '../../store/useProjectStore';
import { outcomeColor, outcomeGlyph, claimStatusColor } from '../../utils/evidence';
import { statusLine } from '../../utils/experiment';
import { fmtDayTime } from '../../utils/format';
import EntityChip from '../EntityChip';
import { useKinHover, useIsKin } from './kinHover';
import { BEAT } from './storyModel';

/**
 * StoryBeat — one sentence of the story, expandable in place to its
 * evidence. The sentence names what happened in plain words; the
 * disclosure carries the why (intent, conclusion, the reviewer's word,
 * the shift's rationale) plus the ways out: open the entity's page, or
 * follow a claim's thread through the whole story.
 */

const CLAIM_CHIP_LIMIT = 2;

function verdictVerb(outcome, hasClaims) {
  switch (outcome) {
    case 'supports': return hasClaims ? 'supported' : 'concluded in favor';
    case 'refutes': return hasClaims ? 'refuted' : 'concluded against';
    case 'qualifies': return hasClaims ? 'left unclear evidence on' : 'ended unclear';
    default: return hasClaims ? 'was abandoned while testing' : 'was abandoned';
  }
}

function ClaimChips({ ids }) {
  const shown = ids.slice(0, CLAIM_CHIP_LIMIT);
  const extra = ids.length - shown.length;
  return (
    <>
      {shown.map(id => <EntityChip key={id} id={id} compact />)}
      {extra > 0 && <span className="story-chips-more muted">+{extra} more</span>}
    </>
  );
}

function markerFor(beat) {
  switch (beat.type) {
    case BEAT.VERDICT: return { glyph: outcomeGlyph(beat.outcome), color: outcomeColor(beat.outcome) };
    case BEAT.LIVE: return { glyph: '◐', color: 'var(--active)', live: true };
    case BEAT.SHIFT: return {
      glyph: '◇',
      color: beat.shift.status ? claimStatusColor(beat.shift.status.to) : 'var(--steel)',
    };
    case BEAT.STAKED: return { glyph: '◇', color: 'var(--steel)' };
    case BEAT.WAVE: return { glyph: '❖', color: 'var(--mcp)' };
    default: return { glyph: '·', color: 'var(--faint)' };
  }
}

function Sentence({ beat }) {
  switch (beat.type) {
    case BEAT.VERDICT:
      return (
        <>
          <b className="story-beat-name">{beat.name}</b>{' '}
          {verdictVerb(beat.outcome, beat.claimIds.length > 0)}{' '}
          {beat.claimIds.length > 0 && <ClaimChips ids={beat.claimIds} />}
        </>
      );
    case BEAT.LIVE:
      return (
        <>
          <b className="story-beat-name">{beat.name}</b>{' '}
          <span className="story-beat-live-line">{statusLine(beat.exp, beat.exp.status, Date.now())}</span>
          {beat.claimIds.length > 0 && <> — testing <ClaimChips ids={beat.claimIds} /></>}
        </>
      );
    case BEAT.SHIFT: {
      const s = beat.shift;
      return (
        <>
          <EntityChip id={s.claimId} compact />{' '}
          {s.status ? (
            <>
              moved {s.status.from} →{' '}
              <b style={{ color: claimStatusColor(s.status.to) }}>{s.status.to}</b>
            </>
          ) : (
            <>confidence {s.confidence.from} → <b>{s.confidence.to}</b></>
          )}
        </>
      );
    }
    case BEAT.STAKED:
      return (
        <>
          <span className="story-beat-quietverb">staked</span>{' '}
          <span className="story-beat-statement">{beat.claim.statement}</span>
        </>
      );
    case BEAT.WAVE: {
      const waveTitle = (beat.wave.title || '').trim();
      return (
        <>
          <b className="story-beat-name">Reflection published</b>
          <span className="story-beat-quietverb">
            {waveTitle ? <> — “{waveTitle}”</> : ' — the project took stock and set the next direction'}
          </span>
        </>
      );
    }
    default:
      return null;
  }
}

function DetailRow({ label, children }) {
  if (!children) return null;
  return (
    <div className="story-detail-row">
      <span className="story-detail-label">{label}</span>
      <span className="story-detail-text">{children}</span>
    </div>
  );
}

function clampText(s, n = 44) {
  const t = (s || '').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

// One follow button per claim, labeled by the claim so multi-claim beats
// offer distinguishable threads.
function FollowButtons({ claims, onFollow }) {
  return (claims || []).filter(c => c?.id).map(c => (
    <button
      key={c.id}
      type="button"
      className="btn btn--sm story-follow-btn"
      onClick={() => onFollow(c.id)}
      title={c.statement || c.id}
    >
      ⌁ Follow {c.statement ? `“${clampText(c.statement)}”` : 'this thread'}
    </button>
  ));
}

// The claims a beat can follow, with statements for button labels.
function followableClaims(beat) {
  switch (beat.type) {
    case BEAT.VERDICT:
    case BEAT.LIVE:
      return (beat.exp.tested_claims || []).map(c => ({ id: c?.id, statement: c?.statement }));
    case BEAT.SHIFT:
      return [{ id: beat.shift.claimId, statement: beat.shift.statement }];
    case BEAT.STAKED:
      return [{ id: beat.claim.id, statement: beat.claim.statement }];
    default:
      return [];
  }
}

function scrollToSynthesis() {
  document.getElementById('project-synthesis')?.scrollIntoView({ block: 'start' });
}

function Detail({ beat, onFollow }) {
  const px = useProjectHref();
  const threads = <FollowButtons claims={followableClaims(beat)} onFollow={onFollow} />;
  switch (beat.type) {
    case BEAT.VERDICT:
    case BEAT.LIVE:
      return (
        <div className="story-detail">
          <DetailRow label="Intent">{(beat.exp.intent || '').trim()}</DetailRow>
          <DetailRow label="Conclusion">{(beat.exp.conclusion || '').trim()}</DetailRow>
          <DetailRow label="Reviewer">{beat.synopsis}</DetailRow>
          <div className="story-detail-actions">
            <Link className="btn btn--sm" to={px(`/experiments/${beat.exp.id}`)}>Open experiment →</Link>
            {threads}
          </div>
        </div>
      );
    case BEAT.SHIFT:
      return (
        <div className="story-detail">
          <DetailRow label="Why">{beat.shift.rationale}</DetailRow>
          <div className="story-detail-actions">
            <Link className="btn btn--sm" to={px(`/claims/${beat.shift.claimId}`)}>Open claim →</Link>
            {threads}
          </div>
        </div>
      );
    case BEAT.STAKED:
      return (
        <div className="story-detail">
          <DetailRow label="Scope">{(beat.claim.scope || '').trim()}</DetailRow>
          <DetailRow label="Confidence">{beat.claim.confidence}</DetailRow>
          <div className="story-detail-actions">
            <Link className="btn btn--sm" to={px(`/claims/${beat.claim.id}`)}>Open claim →</Link>
            {threads}
          </div>
        </div>
      );
    case BEAT.WAVE:
      return (
        <div className="story-detail">
          {beat.wave.attempt_index > 1 && (
            <DetailRow label="Attempts">{`took ${beat.wave.attempt_index} attempts to pass review`}</DetailRow>
          )}
          <div className="story-detail-actions">
            {/* Scroll, not a hash link: repeat clicks keep working and the
                router URL stays clean. The panel shows the CURRENT wave; its
                history strip pans back to this one. */}
            <button type="button" className="btn btn--sm" onClick={scrollToSynthesis}>
              Open synthesis history ↓
            </button>
          </div>
        </div>
      );
    default:
      return null;
  }
}

function StoryBeat({ beat, onFollow }) {
  const [open, setOpen] = useState(false);
  const setKinIds = useKinHover(s => s.setIds);
  const kin = useIsKin(beat.claimIds);
  const marker = markerFor(beat);
  const day = fmtDayTime(beat.atIso)?.day || '';

  const toggle = useCallback((e) => {
    // Links and buttons inside the row keep their own meaning.
    if (e.target.closest('a, button')) return;
    setOpen(v => !v);
  }, []);

  const onEnter = useCallback(() => {
    if (beat.claimIds.length > 0) setKinIds(beat.claimIds);
  }, [beat.claimIds, setKinIds]);
  const onLeave = useCallback(() => { setKinIds(null); }, [setKinIds]);

  return (
    <li
      className={[
        'story-beat',
        `story-beat--${beat.type}`,
        open ? 'story-beat--open' : '',
        kin ? 'story-beat--kin' : '',
      ].filter(Boolean).join(' ')}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <div className="story-beat-row" onClick={toggle}>
        <span
          className={`story-beat-marker${marker.live ? ' story-beat-marker--live' : ''}`}
          style={{ color: marker.color }}
          aria-hidden="true"
        >
          {marker.glyph}
        </span>
        <span className="story-beat-sentence"><Sentence beat={beat} /></span>
        <span className="story-beat-when">{day}</span>
        <button
          type="button"
          className="story-beat-caret"
          onClick={() => setOpen(v => !v)}
          aria-expanded={open}
          aria-label={open ? 'Collapse detail' : 'Expand detail'}
        >
          {open ? '▾' : '▸'}
        </button>
      </div>
      {open && <Detail beat={beat} onFollow={onFollow} />}
    </li>
  );
}

// Memo: beats re-render only when their own beat object, kin flag, or open
// state changes — not when a sibling is hovered.
export default memo(StoryBeat);
