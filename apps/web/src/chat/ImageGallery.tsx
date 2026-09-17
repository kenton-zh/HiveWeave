import { memo, useMemo, useState } from "react";
import type { GalleryImage } from "./messageUtils";
import { ImageLightbox } from "./ImageLightbox";

/**
 * P1 图片 gallery（2026-09-06，学 DSH ui-attachment）：
 * - 单图渲染规则照抄 DSH MessageImage.tsx:45-57 `singleFit`：长边 240px、
 *   显示宽高比 clamp 到 [0.25, 4]（溢出由 object-fit: cover 裁掉）、
 *   **不放大大于自然尺寸**；裁切锚点偏信息密集侧（竖长图贴顶、横宽图贴左）。
 * - 多图 → 固定方格 tile 网格（DSH ImageGallery: lone → large, several →
 *   square tiles；tile 边长从 DSH 的 64px 放大到 96px —— HiveWeave 现有
 *   缩略图量级是 max 200px，64px 观感过小，属记录在案的本地偏差）。
 * - 点击任意图打开 lightbox（ImageLightbox，独立组件）。
 */

/** DSH MessageImage.tsx:45-57 原样移植（数值与锚点规则一致）。 */
export function singleFit(dimensions: {
  readonly width: number;
  readonly height: number;
}): { width: number; height: number; objectPosition: string } {
  const natural = dimensions.width / dimensions.height;
  const ratio = Math.min(4, Math.max(0.25, natural));
  const box = ratio >= 1 ? { width: 240, height: 240 / ratio } : { width: 240 * ratio, height: 240 };
  const scale = Math.min(1, dimensions.width / box.width, dimensions.height / box.height);
  return {
    width: Math.max(1, Math.round(box.width * scale)),
    height: Math.max(1, Math.round(box.height * scale)),
    objectPosition: natural < 0.25 ? "center top" : natural > 4 ? "left center" : "center",
  };
}

const TILE_PX = 96;

export const ImageGallery = memo(function ImageGallery({
  images,
  align = "start",
}: {
  images: GalleryImage[];
  /** 气泡内对齐：user 靠右（end）、assistant 靠左（start），同 DSH gallery。 */
  align?: "start" | "end";
}) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const single = images.length === 1;
  // 单图：有内在尺寸走 singleFit 精确框（未知尺寸按 240 方形裁切，
  // DSH MessageImage.tsx:100-104 同兜底）；多图：固定 tile 方格。
  const fit = useMemo(() => {
    if (!single) return undefined;
    const dims =
      images[0]?.width !== undefined && images[0]?.height !== undefined
        ? { width: images[0].width, height: images[0].height }
        : undefined;
    return dims ? singleFit(dims) : { width: 240, height: 240, objectPosition: "center" };
  }, [single, images]);

  if (images.length === 0) return null;

  return (
    <div
      className={`mt-1.5 mb-1 flex flex-wrap gap-2 ${align === "end" ? "justify-end" : "justify-start"}`}
      data-hw-gallery=""
    >
      {images.map((image, i) => (
        <button
          key={`${image.src}:${i}`}
          type="button"
          title="查看原图"
          aria-label={`查看图片${image.name ? `：${image.name}` : ""}`}
          onClick={() => setOpenIndex(i)}
          className={`shrink-0 cursor-zoom-in overflow-hidden rounded-gm p-0 ${
            isUserSide(align) ? "ring-1 ring-white/30" : "border border-g-border"
          }`}
          style={
            single
              ? { width: fit!.width, height: fit!.height }
              : { width: TILE_PX, height: TILE_PX }
          }
        >
          <img
            src={image.src}
            alt={image.name ?? ""}
            loading="lazy"
            decoding="async"
            className="h-full w-full object-cover"
            style={single ? { objectPosition: fit!.objectPosition } : undefined}
          />
        </button>
      ))}
      {openIndex !== null && (
        <ImageLightbox images={images} index={openIndex} onClose={() => setOpenIndex(null)} />
      )}
    </div>
  );
});

function isUserSide(align: "start" | "end"): boolean {
  return align === "end";
}
