import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

/**
 * Tap-to-zoom viewer for feed media. Deliberately minimal: scrim + image +
 * close. Esc, scrim-click, or the button dismiss it; body scroll is locked
 * while it is open.
 */
export default function Lightbox({ src, alt = '', onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return createPortal(
    <div
      className="feed-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="Image viewer"
      onClick={onClose}
    >
      <img
        src={src}
        alt={alt}
        className="feed-lightbox-img"
        onClick={(e) => e.stopPropagation()}
      />
      <button
        ref={closeRef}
        type="button"
        className="feed-lightbox-close"
        aria-label="Close image viewer"
        onClick={onClose}
      >
        ✕
      </button>
    </div>,
    document.body
  );
}
