import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { feedApi } from './feedApi';
import { postTime } from './feedModel';
import { useProjectStore, useProjectHref, selectExperiments } from '../store/useProjectStore';
import { expName } from '../utils/experiment';

// Load a feed media path through an authenticated fetch and expose it as a
// blob: object URL. Needed because hosted control mode serves feed bytes behind
// the Bearer token, which a plain <img src> can't send. Revokes on unmount /
// path change. Returns null until loaded (or on error → image simply omitted).
function useAuthedImage(relPath) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    if (!relPath) { setUrl(null); return undefined; }
    let active = true;
    let objectUrl = null;
    const controller = new AbortController();
    feedApi.imageObjectUrl(relPath, { signal: controller.signal })
      .then((u) => {
        if (active) { objectUrl = u; setUrl(u); }
        else { URL.revokeObjectURL(u); }
      })
      .catch(() => { if (active) setUrl(null); });
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [relPath]);
  return url;
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
export default function PostCard({ post, projectId, onView, now }) {
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
  const imageSrc = useAuthedImage(post.image_url);
  const linkThumbSrc = useAuthedImage(
    preview && preview.has_image ? preview.image_url : null
  );

  return (
    <article className="postcard" ref={cardRef}>
      <header className="postcard-head">
        <span className="postcard-author">{post.author_handle}</span>
        {post.author_role && post.author_role !== 'main' && (
          <span className="postcard-role">{post.author_role}</span>
        )}
        {timeLabel && (
          <span className="postcard-time" title={ts != null ? new Date(ts).toLocaleString() : undefined}>
            {timeLabel}
          </span>
        )}
      </header>

      {post.text && <p className="postcard-text">{post.text}</p>}

      {post.image_url && imageSrc && (
        <div className="postcard-media">
          <img
            src={imageSrc}
            alt=""
            loading="lazy"
            className="postcard-image"
          />
        </div>
      )}

      {post.link_url && preview && !preview.error && (
        <a
          className="postcard-link"
          href={post.link_url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          onClick={() => feedApi.trackFeed(projectId, 'link_clicked', { post_id: post.id }).catch(() => {})}
        >
          {preview.has_image && linkThumbSrc && (
            <img src={linkThumbSrc} alt="" loading="lazy" className="postcard-link-thumb" />
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
