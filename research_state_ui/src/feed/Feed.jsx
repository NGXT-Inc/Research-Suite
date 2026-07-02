import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useProjectStore } from '../store/useProjectStore';
import { feedApi } from './feedApi';
import PostCard from './PostCard';
import { useNow, dayLabel, withDayDividers } from './feedModel';
import './feed.css';

const PAGE_SIZE = 20;
const POLL_MS = 10000;

/**
 * The social feed (Feed_PRD.md): a reverse-chronological, low-chrome stream of
 * the agents' aha-moments. Shared by desktop (/feed sidebar route) and mobile
 * (/feed, the first bottom-nav tab). Infinite scroll downward (older), a light
 * poll upward (newer), and fire-and-forget usage analytics.
 *
 * Self-contained: depends only on the shared project store + the feed's own
 * api module. Nothing else in the product reaches into the feed.
 */
export default function Feed() {
  const projectId = useProjectStore(s => s.projectId);
  const [posts, setPosts] = useState([]);
  const [pending, setPending] = useState([]); // polled posts held behind the pill
  const [lastSeenSeq, setLastSeenSeq] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [status, setStatus] = useState('loading'); // loading | ready | error
  const [error, setError] = useState('');
  const loadingMoreRef = useRef(false);
  const sentinelRef = useRef(null);
  // Newest seq across visible + buffered posts, so the poll closure never
  // re-fetches what it already holds.
  const topSeqRef = useRef(0);
  useEffect(() => {
    topSeqRef.current = pending[0]?.created_seq ?? posts[0]?.created_seq ?? 0;
  }, [posts, pending]);

  const seenKey = projectId ? `rsui:feed:lastSeen:${projectId}` : null;

  // Initial load (and reload on project switch).
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setStatus('loading');
    setPosts([]);
    setPending([]);
    setCursor(null);
    setHasMore(false);
    // Where the previous visit ended — frozen for this session so the marker
    // doesn't chase the reader down the page.
    const stored = Number(localStorage.getItem(`rsui:feed:lastSeen:${projectId}`));
    setLastSeenSeq(Number.isFinite(stored) && stored > 0 ? stored : null);
    feedApi.getFeed(projectId, { limit: PAGE_SIZE })
      .then((data) => {
        if (cancelled) return;
        setPosts(data.posts || []);
        setCursor(data.next_cursor ?? null);
        setHasMore(data.next_cursor != null);
        setStatus('ready');
        feedApi.trackFeed(projectId, 'feed_opened', { count: (data.posts || []).length }).catch(() => {});
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.message || 'Failed to load feed');
        setStatus('error');
      });
    return () => { cancelled = true; };
  }, [projectId]);

  // Remember the newest post the reader actually had on screen, for the next
  // visit's "new since last visit" marker. Buffered posts don't count until
  // they are revealed.
  useEffect(() => {
    if (status !== 'ready' || !seenKey) return;
    const top = posts[0]?.created_seq;
    if (top == null) return;
    const stored = Number(localStorage.getItem(seenKey)) || 0;
    if (top > stored) localStorage.setItem(seenKey, String(top));
  }, [posts, status, seenKey]);

  // Poll for newer posts. Fresh posts are buffered, never prepended straight
  // into the list — a stream that shoves content under the reader teaches them
  // not to trust their scroll position. The pill releases the buffer; when the
  // reader is already at the top, it releases itself.
  useEffect(() => {
    if (!projectId || status !== 'ready') return;
    const t = setInterval(() => {
      feedApi.getFeed(projectId, { limit: PAGE_SIZE })
        .then((data) => {
          const fresh = (data.posts || []).filter((p) => p.created_seq > topSeqRef.current);
          if (fresh.length) setPending((prev) => [...fresh, ...prev]);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
  }, [projectId, status]);

  const revealPending = useCallback((scroll) => {
    setPending((buffered) => {
      if (buffered.length) {
        setPosts((prev) => {
          const seen = new Set(prev.map((p) => p.id));
          return [...buffered.filter((p) => !seen.has(p.id)), ...prev];
        });
      }
      return [];
    });
    if (scroll) window.scrollTo({ top: 0 });
  }, []);

  // At (or near) the top, new posts just appear — the pill is only for readers
  // who have scrolled into the past.
  useEffect(() => {
    if (pending.length && window.scrollY <= 80) revealPending(false);
  }, [pending, revealPending]);

  const loadMore = useCallback(() => {
    if (!projectId || loadingMoreRef.current || cursor == null) return;
    loadingMoreRef.current = true;
    feedApi.getFeed(projectId, { limit: PAGE_SIZE, cursor })
      .then((data) => {
        const older = data.posts || [];
        setPosts((prev) => {
          const seen = new Set(prev.map((p) => p.id));
          return [...prev, ...older.filter((p) => !seen.has(p.id))];
        });
        setCursor(data.next_cursor ?? null);
        setHasMore(data.next_cursor != null);
      })
      .catch(() => {})
      .finally(() => { loadingMoreRef.current = false; });
  }, [projectId, cursor]);

  // Infinite scroll: load older when the sentinel scrolls into view.
  useEffect(() => {
    if (!hasMore || !sentinelRef.current) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMore();
    }, { rootMargin: '400px' });
    io.observe(sentinelRef.current);
    return () => io.disconnect();
  }, [hasMore, loadMore, posts.length]);

  const onView = useCallback((postId) => {
    if (projectId) feedApi.trackFeed(projectId, 'post_viewed', { post_id: postId }).catch(() => {});
  }, [projectId]);

  // One shared clock: every card's relative time ages in step, and the day
  // dividers roll over correctly at midnight.
  const now = useNow();
  const items = useMemo(() => withDayDividers(posts, now, lastSeenSeq), [posts, now, lastSeenSeq]);

  return (
    <div className="feed-stage">
      {/* Visually hidden on desktop; the mobile surface styles it as the
          page title (One-Surface redesign). */}
      <h1 className="feed-title">Feed</h1>
      <div className="feed-newpill-wrap" aria-live="polite">
        {pending.length > 0 && (
          <button type="button" className="feed-newpill" onClick={() => revealPending(true)}>
            ↑ {pending.length} new post{pending.length === 1 ? '' : 's'}
          </button>
        )}
      </div>
      {status === 'loading' && <div className="feed-note">Loading feed…</div>}
      {status === 'error' && <div className="error-message feed-note">{error}</div>}
      {status === 'ready' && posts.length === 0 && (
        <div className="feed-empty">
          <p className="feed-empty-title">No posts yet</p>
        </div>
      )}
      {posts.length > 0 && (
        <div className="feed-list">
          {items.map((item) => {
            if (item.type === 'day') {
              return (
                <div key={item.id} className="feed-day" role="separator">
                  {dayLabel(item.ts, now)}
                </div>
              );
            }
            if (item.type === 'unseen') {
              return (
                <div key={item.id} className="feed-unseen" role="separator">
                  new since your last visit
                </div>
              );
            }
            return (
              <PostCard key={item.id} post={item.post} projectId={projectId} onView={onView} now={now} />
            );
          })}
        </div>
      )}
      {hasMore && (
        <div ref={sentinelRef} className="feed-sentinel">
          <button type="button" className="btn btn--ghost btn--sm" onClick={loadMore}>Load older</button>
        </div>
      )}
    </div>
  );
}
