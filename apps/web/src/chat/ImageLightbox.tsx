import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { GalleryImage } from "./messageUtils";

/**
 * P1 图片 lightbox（2026-09-06，学 DSH ui-attachment/ImageLightbox.tsx）：
 * 文档级原图预览 —— 全屏遮罩 + 居中大图 + ←→ 切换 + Esc / 点遮罩关闭 +
 * 图片计数 1/N。走 body portal：气玻璃泡祖先若有 transform/filter，fixed
 * 遮罩会被困在该祖先盒内盖不满视口（DSH ImageLightbox.tsx:19 注释同因）。
 *
 * 键盘：Escape 关闭；←/→ 在边界停住（不回绕，计数始终诚实）。
 * 焦点：挂载时聚焦关闭钮，卸载时还原到打开者（DSH 同款）。
 */

function LightboxNavButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="flex h-10 w-10 items-center justify-center rounded-full bg-white/10 text-white/90 transition-colors hover:bg-white/20"
    >
      {children}
    </button>
  );
}

export function ImageLightbox({
  images,
  index,
  onClose,
}: {
  images: GalleryImage[];
  /** 打开时的起始下标（0 起号）。 */
  index: number;
  onClose: () => void;
}) {
  const [current, setCurrent] = useState(() =>
    Math.min(Math.max(index, 0), Math.max(images.length - 1, 0)),
  );
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => {
      restoreRef.current?.focus();
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "ArrowLeft") {
        setCurrent((c) => Math.max(0, c - 1));
      } else if (e.key === "ArrowRight") {
        setCurrent((c) => Math.min(images.length - 1, c + 1));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, images.length]);

  const goPrev = useCallback(() => setCurrent((c) => Math.max(0, c - 1)), []);
  const goNext = useCallback(
    () => setCurrent((c) => Math.min(images.length - 1, c + 1)),
    [images.length],
  );

  const image = images[current];
  if (!image) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`图片预览 ${current + 1} / ${images.length}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
      onClick={onClose}
    >
      {/* 居中大图：max 限制在视口内，点击图片本身不冒泡关闭 */}
      <img
        src={image.src}
        alt={image.name ?? ""}
        className="max-h-[85vh] max-w-[90vw] rounded-gm shadow-gm-pop select-none"
        onClick={(e) => e.stopPropagation()}
      />
      {/* 左右切换（单图时隐藏） */}
      {images.length > 1 && (
        <>
          {current > 0 && (
            <div className="absolute left-3 top-1/2 -translate-y-1/2">
              <LightboxNavButton label="上一张" onClick={goPrev}>
                <svg
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  aria-hidden="true"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
                </svg>
              </LightboxNavButton>
            </div>
          )}
          {current < images.length - 1 && (
            <div className="absolute right-3 top-1/2 -translate-y-1/2">
              <LightboxNavButton label="下一张" onClick={goNext}>
                <svg
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  aria-hidden="true"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
              </LightboxNavButton>
            </div>
          )}
        </>
      )}
      {/* 顶部：计数 + 关闭 */}
      <div className="absolute left-1/2 top-4 -translate-x-1/2 rounded-full bg-black/40 px-3 py-1 text-xs font-medium text-white/90 select-none">
        {current + 1} / {images.length}
        {image.name ? ` · ${image.name}` : ""}
      </div>
      <button
        ref={closeRef}
        type="button"
        aria-label="关闭图片预览"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        className="absolute right-4 top-4 flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-white/90 transition-colors hover:bg-white/20"
      >
        <svg
          className="h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>,
    document.body,
  );
}
