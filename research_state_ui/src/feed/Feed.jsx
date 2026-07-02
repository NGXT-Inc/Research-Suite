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
  const [cursor, setCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [status, setStatus] = useState('loading'); // loading | ready | error
  const [error, setError] = useState('');
  const loadingMoreRef = useRef(false);
  const sentinelRef = useRef(null);

  // Initial load (and reload on project switch).
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setStatus('loading');
    setPosts([]);
    setCursor(null);
    setHasMore(false);
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

  // Poll for newer posts (prepend, deduped) without disturbing scroll position.
  useEffect(() => {
    if (!projectId || status !== 'ready') return;
    const t = setInterval(() => {
      feedApi.getFeed(projectId, { limit: PAGE_SIZE })
        .then((data) => {
          const incoming = data.posts || [];
          setPosts((prev) => {
            if (!prev.length) return incoming;
            const top = prev[0].created_seq;
            const fresh = incoming.filter((p) => p.created_seq > top);
            return fresh.length ? [...fresh, ...prev] : prev;
          });
        })
        .catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
  }, [projectId, status]);

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
  const items = useMemo(() => withDayDividers(posts, now), [posts, now]);

  return (
    <div className="feed-stage">
      {/* Visually hidden on desktop; the mobile surface styles it as the
          page title (One-Surface redesign). */}
      <h1 className="feed-title">Feed</h1>
      {status === 'loading' && <div className="feed-note">Loading feed…</div>}
      {status === 'error' && <div className="error-message feed-note">{error}</div>}
      {status === 'ready' && posts.length === 0 && (
        <div className="feed-empty">
          <p className="feed-empty-title">No posts yet</p>
        </div>
      )}
      {posts.length > 0 && (
        <div className="feed-list">
          {items.map((item) => (
            item.type === 'day' ? (
              <div key={item.id} className="feed-day" role="separator">
                {dayLabel(item.ts, now)}
              </div>
            ) : (
              <PostCard key={item.id} post={item.post} projectId={projectId} onView={onView} now={now} />
            )
          ))}
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
