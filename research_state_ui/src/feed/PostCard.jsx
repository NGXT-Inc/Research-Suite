import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { feedApi } from './feedApi';
import { postTime } from './feedModel';
import Lightbox from './Lightbox';
import { useProjectStore, useProjectHref, selectExperiments } from '../store/useProjectStore';
import { expName } from '../utils/experiment';
import { authorColor } from '../utils/authorIdentity';

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

// Map a post's optional entity ref to the route that shows it. Experiments
// resolve to their display name (the chip should read "↗ vision-scaling",
// not "↗ experiment"). Unknown kinds (rver_/rev_/syn_) render as a static
// chip — there is no single detail page for them.
function refTarget(ref, experiments) {
  if (!ref) return null;
  if (ref.startsWith('exp_')) {
    const exp = experiments.find(e => e.id === ref);
    return { to: `/experiments/${ref}`, label: exp ? expName(exp) : 'experiment' };
  }
  if (ref.startsWith('claim_')) return { to: `/claims/${ref}`, label: 'claim' };
  if (ref.startsWith('res_')) return { to: `/resources/${ref}`, label: 'resource' };
  return null;
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}

/**
 * One feed post (Feed_PRD.md): handle + relative time, brief text, an optional
 * single visual (image or a static unfurled link card), and an optional chip
 * linking to the entity it is about. Deliberately low-chrome — content first.
 */
export default function PostCard({ post, projectId, onView, now, grouped = false }) {
  const px = useProjectHref();
  const experiments = useProjectStore(selectExperiments);
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
  const ref = refTarget(post.ref, experiments);
  const preview = post.link_preview;
  const image = useAuthedImage(post.image_url);
  const linkThumb = useAuthedImage(
    preview && preview.has_image ? preview.image_url : null
  );
  const [imageLoaded, setImageLoaded] = useState(false);
  const [zoomed, setZoomed] = useState(false);

  const openZoom = () => {
    setZoomed(true);
    feedApi.trackFeed(projectId, 'image_viewed', { post_id: post.id }).catch(() => {});
  };

  return (
    <article className={`postcard${grouped ? ' postcard--cont' : ''}`} ref={cardRef}>
      {/* A continuation post (same author, moments later) drops the byline —
          the missing header is what reads as "…and then they added". */}
      <header className="postcard-head">
        {!grouped && (
          <span className="postcard-author" style={{ color: authorColor(post.author_handle) }}>
            {post.author_handle}
          </span>
        )}
        {!grouped && post.author_role && post.author_role !== 'main' && (
          <span className={`postcard-role postcard-role--${post.author_role}`}>{post.author_role}</span>
        )}
        {timeLabel && (
          <span className="postcard-time" title={ts != null ? new Date(ts).toLocaleString() : undefined}>
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
        <Lightbox src={image.url} onClose={() => setZoomed(false)} />
      )}

      {post.link_url && preview && !preview.error && (
        <a
          className="postcard-link"
          href={post.link_url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          onClick={() => feedApi.trackFeed(projectId, 'link_clicked', { post_id: post.id }).catch(() => {})}
        >
          {preview.has_image && linkThumb.url && (
            <img src={linkThumb.url} alt="" loading="lazy" className="postcard-link-thumb" />
          )}
          <span className="postcard-link-body">
            <span className="postcard-link-host">
              {hostOf(preview.url || post.link_url)}
              {preview.trusted && <span className="postcard-link-trusted" title="known research source">✓</span>}
            </span>
            {preview.title && <span className="postcard-link-title">{preview.title}</span>}
            {preview.description && <span className="postcard-link-desc">{preview.description}</span>}
          </span>
        </a>
      )}

      {post.link_url && (!preview || preview.error) && (
        <a
          className="postcard-link postcard-link--bare"
          href={post.link_url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          onClick={() => feedApi.trackFeed(projectId, 'link_clicked', { post_id: post.id }).catch(() => {})}
        >
          {post.link_url}
        </a>
      )}

      {post.ref && (
        <footer className="postcard-foot">
          {ref ? (
            <Link className="postcard-ref" to={px(ref.to)}>↗ {ref.label}</Link>
          ) : (
            <span className="postcard-ref postcard-ref--static">↗ {post.ref}</span>
          )}
        </footer>
      )}
    </article>
  );
}
