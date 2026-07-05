import { useCallback, useEffect, useRef, useState } from 'react';
import { feedApi } from './feedApi';
import { postTime } from './feedModel';
import Lightbox from './Lightbox';
import LinkCard from './LinkCard';
import EntityChip from '../components/EntityChip';
import { authorHue } from '../utils/authorIdentity';

// Load a feed media path through an authenticated fetch and expose it as a
// blob: object URL. Needed because hosted control mode serves feed bytes behind
// the Bearer token, which a plain <img src> can't send. Revokes on unmount /
// path change. `failed` lets the card collapse a media box that will never
// fill, instead of leaving a permanently empty slab.
function useAuthedImage(relPath) {
  const [state, setState] = useState({ url: null, failed: false });
  useEffect(() => {
    if (!relPath) { setState({ url: null, failed: false }); return undefined; }
    let active = true;
    let objectUrl = null;
    const controller = new AbortController();
    setState({ url: null, failed: false });
    feedApi.imageObjectUrl(relPath, { signal: controller.signal })
      .then((u) => {
        if (active) { objectUrl = u; setState({ url: u, failed: false }); }
        else { URL.revokeObjectURL(u); }
      })
      .catch(() => { if (active) setState({ url: null, failed: true }); });
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [relPath]);
  return state;
}

/**
 * One feed post (Feed_PRD.md): handle + relative time, brief text, an optional
 * single visual (image or a static unfurled link card), and an optional chip
 * linking to the entity it is about. Deliberately low-chrome — content first.
 */
export default function PostCard({ post, projectId, onView, now, grouped = false }) {
  const cardRef = useRef(null);
  const viewedRef = useRef(false);

  // Fire post_viewed once, when the card first enters the viewport.
  useEffect(() => {
    if (!onView || !cardRef.current || viewedRef.current) return;
    const el = cardRef.current;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting && !viewedRef.current) {
          viewedRef.current = true;
          onView(post.id);
          io.disconnect();
        }
      }
    }, { threshold: 0.5 });
    io.observe(el);
    return () => io.disconnect();
  }, [post.id, onView]);

  const ts = post.created_at ? new Date(post.created_at).getTime() : null;
  const timeLabel = postTime(ts, now);
  const preview = post.link_preview;
  const image = useAuthedImage(post.image_url);
  const linkThumb = useAuthedImage(
    preview && preview.has_image ? preview.image_url : null
  );
  const [imageLoaded, setImageLoaded] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const mediaBtnRef = useRef(null);

  const openZoom = () => {
    setZoomed(true);
    feedApi.trackFeed(projectId, 'image_viewed', { post_id: post.id }).catch(() => {});
  };
  // Stable identity: it sits in the Lightbox effect deps, and this card
  // re-renders every clock tick.
  const closeZoom = useCallback(() => {
    setZoomed(false);
    mediaBtnRef.current?.focus();
  }, []);

  const kind = post.kind || null;

  return (
    <article
      className={`postcard${grouped ? ' postcard--cont' : ''}${kind ? ` postcard--${kind}` : ''}`}
      ref={cardRef}
    >
      {/* A continuation post (same author, moments later) visually drops the
          byline — the missing header is what reads as "…and then they added".
          It stays in the DOM so the article keeps its attribution for
          screen readers. */}
      <header className={`postcard-head${grouped ? ' postcard-head--cont' : ''}`}>
        <span
          className="postcard-author"
          style={{ '--author-hue': authorHue(post.author_handle) }}
        >
          {post.author_handle}
        </span>
        {post.author_role && post.author_role !== 'main' && (
          <span className={`postcard-role postcard-role--${post.author_role}`}>{post.author_role}</span>
        )}
        {/* The kind names the accent for non-color users; it survives
            continuation posts because it is per-post, not per-author. */}
        {kind && <span className={`postcard-kind postcard-kind--${kind}`}>{kind}</span>}
        {timeLabel && (
          <span
            className="postcard-time"
            title={Number.isFinite(ts) ? new Date(ts).toLocaleString() : undefined}
          >
            {timeLabel}
          </span>
        )}
      </header>

      {post.text && <p className="postcard-text">{post.text}</p>}

      {/* The media box is reserved as soon as we know a post has an image, so
          the stream never jumps when blobs arrive; it collapses only if the
          fetch actually fails. */}
      {post.image_url && !image.failed && (
        <div className="postcard-media">
          <button
            ref={mediaBtnRef}
            type="button"
            className="postcard-media-btn"
            onClick={openZoom}
            disabled={!image.url}
            aria-label="View image full size"
          >
            {image.url && (
              <img
                src={image.url}
                alt=""
                className={`postcard-image${imageLoaded ? ' is-loaded' : ''}`}
                onLoad={() => setImageLoaded(true)}
              />
            )}
          </button>
        </div>
      )}
      {zoomed && image.url && (
        <Lightbox src={image.url} onClose={closeZoom} />
      )}

      {post.link_url && (
        <LinkCard post={post} preview={preview} thumbUrl={linkThumb.url} projectId={projectId} />
      )}

      {post.ref && (
        <footer className="postcard-foot">
          <EntityChip id={post.ref} className="postcard-ref-chip" />
        </footer>
      )}
    </article>
  );
}
