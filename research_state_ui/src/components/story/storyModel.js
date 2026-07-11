/**
 * storyModel — derives the Research Story from records the store already
 * holds. Pure helpers, no JSX, no fetching.
 *
 * The story is chapters and beats. Published reflection waves are the act
 * breaks: chapter N is everything that happened up to (and including) wave
 * N's publish; the tail past the last published wave is the always-live
 * "Now" chapter. Beats are the sentences inside a chapter — an experiment
 * reaching a verdict, a belief shifting, a claim being staked, a wave
 * publishing. Everything is DERIVED from state (claims, experiments, the
 * event window, reflections); nothing here is authored.
 *
 * One honest limitation: SHIFT beats come from the polled event window,
 * which the server caps at 500 events, so shifts older than the window are
 * absent (not nonexistent). `shiftHorizon` reports the window's oldest
 * event so the UI can say so instead of silently under-narrating history.
 */

import { classifyExperiment, latestExperimentReview } from '../../utils/evidence';
import { computeClaimShifts } from '../../utils/claimShifts';
import { parseTs } from '../../utils/format';
import { TERMINAL_STATUSES, expName } from '../../utils/experiment';
import { TERMINAL_WAVE } from '../synthesis/waveModel';

export const BEAT = Object.freeze({
  STAKED: 'staked',        // a claim entered the record
  VERDICT: 'verdict',      // an experiment reached a terminal outcome
  LIVE: 'live',            // an experiment still moving (Now chapter only)
  SHIFT: 'shift',          // a claim's status/confidence moved
  WAVE: 'wave',            // a reflection wave published — the chapter closer
});

// Publishing a wave materializes its change spec AFTER published_at is
// stamped (both at second resolution), so the wave's own claim events can
// trail its publish time by a beat. Event-derived beats inside this grace
// still belong to the chapter the wave closes.
const PUBLISH_GRACE_MS = 2500;

function claimIdsOfExperiment(exp) {
  return (Array.isArray(exp.tested_claims) ? exp.tested_claims : [])
    .map(c => c?.id)
    .filter(Boolean);
}

/** Every beat carries: type, at (ms), atIso, claimIds (for thread follow). */
function makeBeats({ claims, experiments, events, waves }) {
  const beats = [];

  for (const claim of claims || []) {
    const at = parseTs(claim.created_at);
    if (at == null) continue;
    beats.push({
      type: BEAT.STAKED,
      at,
      atIso: claim.created_at,
      key: `staked:${claim.id}`,
      claim,
      claimIds: [claim.id],
    });
  }

  for (const exp of experiments || []) {
    const terminal = TERMINAL_STATUSES.includes(exp.status);
    const review = terminal ? latestExperimentReview(exp) : null;
    // Verdict time: the verdict-bearing review when there is one — the row's
    // updated_at keeps moving on later touches (mlflow refresh, metadata),
    // which would silently migrate the beat across chapter boundaries.
    const atIso = (terminal
      ? (review?.created_at || exp.updated_at)
      : exp.created_at) || exp.created_at;
    const at = parseTs(atIso);
    if (at == null) continue;
    beats.push({
      type: terminal ? BEAT.VERDICT : BEAT.LIVE,
      at,
      atIso,
      key: `exp:${exp.id}`,
      exp,
      name: expName(exp),
      outcome: classifyExperiment(exp),
      synopsis: (review?.synopsis || '').trim() || null,
      claimIds: claimIdsOfExperiment(exp),
    });
  }

  // Oldest-first for chapter assignment (computeClaimShifts returns newest-first).
  const shifts = computeClaimShifts(events || []).slice().reverse();
  shifts.forEach((s, i) => {
    const at = parseTs(s.at);
    if (at == null) return;
    beats.push({
      type: BEAT.SHIFT,
      at,
      atIso: s.at,
      key: `shift:${s.claimId}:${s.at}:${i}`,
      shift: s,
      claimIds: [s.claimId],
    });
  });

  for (const wave of waves || []) {
    const at = parseTs(wave.published_at);
    if (at == null) continue; // unpublished waves are not act breaks (yet)
    beats.push({
      type: BEAT.WAVE,
      at,
      atIso: wave.published_at,
      key: `wave:${wave.id}`,
      wave,
      claimIds: [],
    });
  }

  return beats;
}

// Sort beats chronologically; the wave beat closes its chapter, so on a
// timestamp tie it sorts last.
function beatOrder(a, b) {
  if (a.at !== b.at) return a.at - b.at;
  if ((a.type === BEAT.WAVE) !== (b.type === BEAT.WAVE)) {
    return a.type === BEAT.WAVE ? 1 : -1;
  }
  return a.key.localeCompare(b.key);
}

function emptyTally() {
  return { verdicts: 0, supports: 0, refutes: 0, qualifies: 0, abandoned: 0, shifts: 0, staked: 0, live: 0 };
}

function tallyBeat(tally, beat) {
  if (beat.type === BEAT.VERDICT) {
    tally.verdicts += 1;
    if (beat.outcome === 'supports') tally.supports += 1;
    else if (beat.outcome === 'refutes') tally.refutes += 1;
    else if (beat.outcome === 'qualifies') tally.qualifies += 1;
    else tally.abandoned += 1;
  } else if (beat.type === BEAT.SHIFT) tally.shifts += 1;
  else if (beat.type === BEAT.STAKED) tally.staked += 1;
  else if (beat.type === BEAT.LIVE) tally.live += 1;
}

/** One plain-words sentence summarizing a chapter — the collapsed headline. */
function chapterHeadline(tally, { isNow = false } = {}) {
  const parts = [];
  if (tally.verdicts > 0) {
    const mix = [
      tally.supports > 0 && `${tally.supports} supported`,
      tally.refutes > 0 && `${tally.refutes} refuted`,
      tally.qualifies > 0 && `${tally.qualifies} unclear`,
      tally.abandoned > 0 && `${tally.abandoned} abandoned`,
    ].filter(Boolean).join(', ');
    parts.push(`${tally.verdicts} experiment${tally.verdicts === 1 ? '' : 's'} concluded${mix ? ` — ${mix}` : ''}`);
  }
  if (tally.live > 0) parts.push(`${tally.live} in flight`);
  if (tally.shifts > 0) parts.push(`${tally.shifts} belief${tally.shifts === 1 ? '' : 's'} shifted`);
  if (tally.staked > 0) parts.push(`${tally.staked} claim${tally.staked === 1 ? '' : 's'} staked`);
  if (parts.length === 0) return isNow ? 'Quiet — waiting for the next move' : 'A quiet stretch';
  return parts.join(' · ');
}

/**
 * Build the story.
 *
 * Returns { chapters, beatCount, shiftHorizonIso }.
 * Each chapter: { id, index (1-based), isNow, title, waveTitle, startIso,
 * endIso, beats, tally, headline, wave (the closing wave, if any),
 * openWave (Now only) }.
 *
 * Live experiments always narrate under Now regardless of when they were
 * created — they are current work, not history — and Now's span starts at
 * the last act break, not at the oldest live experiment.
 */
export function buildStory({ claims, experiments, events, waves }) {
  const allWaves = Array.isArray(waves) ? waves : [];
  const published = allWaves
    .filter(w => parseTs(w.published_at) != null)
    .sort((a, b) => parseTs(a.published_at) - parseTs(b.published_at));
  const boundaries = published.map(w => parseTs(w.published_at));
  // A wave still moving through its own FSM (reflect → synthesize → review)
  // narrates as the Now chapter's "reflection in progress" banner.
  const openWave = allWaves.find(w => !TERMINAL_WAVE.has(String(w.status))) || null;

  const beats = makeBeats({ claims, experiments, events, waves: published }).sort(beatOrder);

  const chapters = [];
  for (let i = 0; i <= published.length; i += 1) {
    const isNow = i === published.length;
    chapters.push({
      id: isNow ? 'now' : `ch-${published[i].id}`,
      index: i + 1,
      isNow,
      wave: isNow ? null : published[i],
      openWave: isNow ? openWave : null,
      beats: [],
      tally: emptyTally(),
    });
  }

  // Walk beats oldest→newest, advancing to the next chapter when we pass a
  // published wave's timestamp. The wave beat itself closes its chapter, and
  // event-derived beats get the publish grace (their events can be stamped
  // moments after published_at even though the wave caused them).
  let ci = 0;
  for (const beat of beats) {
    if (beat.type === BEAT.LIVE) {
      const now = chapters[chapters.length - 1];
      now.beats.push(beat);
      tallyBeat(now.tally, beat);
      continue;
    }
    const grace = (beat.type === BEAT.SHIFT || beat.type === BEAT.STAKED) ? PUBLISH_GRACE_MS : 0;
    while (ci < boundaries.length && beat.at > boundaries[ci] + grace) ci += 1;
    const chapter = chapters[Math.min(ci, chapters.length - 1)];
    chapter.beats.push(beat);
    tallyBeat(chapter.tally, beat);
    if (beat.type === BEAT.WAVE) ci += 1;
  }
  // Live beats were appended out of order relative to Now's other beats.
  chapters[chapters.length - 1].beats.sort(beatOrder);

  const lastWave = published[published.length - 1] || null;
  for (const chapter of chapters) {
    const first = chapter.beats[0] || null;
    const last = chapter.beats[chapter.beats.length - 1] || null;
    if (chapter.isNow) {
      // Now opens where the last act closed — not at the oldest live
      // experiment, which can predate the previous chapter's end.
      chapter.startIso = lastWave ? lastWave.published_at : (first ? first.atIso : null);
      chapter.endIso = null;
    } else {
      chapter.startIso = first ? first.atIso : null;
      chapter.endIso = last ? last.atIso : null;
    }
    chapter.headline = chapterHeadline(chapter.tally, { isNow: chapter.isNow });
    chapter.title = chapter.isNow ? 'Now' : `Chapter ${chapter.index}`;
    // The wave's own headline names the act — the agent titled it when it
    // took stock, so it is the closest thing the story has to a chapter name.
    chapter.waveTitle = (chapter.wave?.title || '').trim() || null;
  }

  // Oldest event still in the polled window: shifts before this exist in the
  // record but not in the story (the server caps the window at 500 events).
  let shiftHorizonIso = null;
  for (const ev of events || []) {
    if (ev?.created_at && (!shiftHorizonIso || ev.created_at < shiftHorizonIso)) {
      shiftHorizonIso = ev.created_at;
    }
  }

  return { chapters, beatCount: beats.length, shiftHorizonIso };
}

/** Does this beat belong to the followed claim's thread? */
export function beatInThread(beat, claimId) {
  return beat.claimIds.includes(claimId);
}
