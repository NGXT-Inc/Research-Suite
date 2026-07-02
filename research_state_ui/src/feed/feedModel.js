// Presentation model for the feed's sense of time. One shared clock drives
// every relative timestamp so the whole page ages in step (and stale "2m ago"
// labels can't linger), and day dividers give the stream a calendar rhythm.
import { useEffect, useState } from 'react';
import { fmtAgo } from '../utils/format';

// Shared ticking clock. One instance lives in Feed and flows down as a prop.
export function useNow(intervalMs = 30000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

function dayKey(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

const DAY_MS = 86_400_000;

export function dayLabel(ts, now) {
  if (dayKey(ts) === dayKey(now)) return 'Today';
  if (dayKey(ts) === dayKey(now - DAY_MS)) return 'Yesterday';
  const d = new Date(ts);
  const sameYear = d.getFullYear() === new Date(now).getFullYear();
  return d.toLocaleDateString([], {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

// A post's timestamp: relative while it is from today ("5m ago"); on older
// days the divider already names the date, so just the clock time ("2:05 PM").
export function postTime(ts, now) {
  if (ts == null) return '';
  if (dayKey(ts) === dayKey(now)) return fmtAgo(now - ts);
  return new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

// Interleave day dividers into a newest-first post list. The leading "Today"
// divider is skipped (a feed that opens on today needs no announcement); any
// other day change gets one, including a non-today first group.
//
// lastSeenSeq (optional) marks where the previous visit ended: one `unseen`
// item lands between the newest already-seen post and everything above it.
// No marker when nothing is new, or when nothing was seen before (first visit).
export function withDayDividers(posts, now, lastSeenSeq = null) {
  const items = [];
  let prevKey = dayKey(now);
  let unseenPlaced = lastSeenSeq == null || (posts.length > 0 && posts[0].created_seq <= lastSeenSeq);
  for (const post of posts) {
    if (!unseenPlaced && post.created_seq <= lastSeenSeq) {
      items.push({ type: 'unseen', id: 'unseen' });
      unseenPlaced = true;
    }
    const ts = post.created_at ? Date.parse(post.created_at) : NaN;
    if (Number.isFinite(ts)) {
      const key = dayKey(ts);
      if (key !== prevKey) {
        items.push({ type: 'day', id: `day-${key}`, ts });
        prevKey = key;
      }
    }
    items.push({ type: 'post', id: post.id, post });
  }
  return items;
}
