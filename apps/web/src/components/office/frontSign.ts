/**
 * 前台招牌 —— 运行时按项目名绘制「弧面弯曲」文字。
 *
 * 背景（2026-09-23 用户钦定）：
 *   柜台蓝牌是弧形（随柜台圆柱面弯曲），平面文字盖上去必假，
 *   必须沿弧面逐列弯曲才能与画风一体。
 *
 * 算法（与离线标定脚本 `tasks/frontdesk-extract/sign_name.py` 同源，
 * 曲线参数唯一权威 = `constants.ts` 的 `FRONT_SIGN`）：
 *   1. 把项目名画成一张**水平**文字画布（展平坐标系）
 *   2. 逐列把该画布的像素拉伸到 `frontSignHeight(x)` 的高度，
 *      并放在 `frontSignTop(x)` 处 —— 等价于把弧面展平、写字、再卷回
 *   3. 输出为一张与净牌同尺寸的 canvas，由调用方作为 texture 覆盖在柜台 sprite 上
 *
 * 性能：仅当项目名变化时重算一次（约 145 次 drawImage），摊到切换动作上可忽略。
 */
import { FRONT_SIGN, frontSignTop, frontSignHeight } from "./constants";

/** 面板内部使用的 canvas 尺寸（= 净牌 sprite 像素尺寸） */
export const FRONT_SIGN_CANVAS = { w: 284, h: 158 } as const;

/** 文字在展平坐标系里的最大宽度占比（净牌左右留白，避免压到白描边） */
const TEXT_WIDTH_RATIO = 0.88;
/** 字体族：中文优先，回退到系统无衬线 */
const FONT_FAMILY =
  '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif';

/**
 * 生成「带项目名的招牌」canvas。
 *
 * @param baseImage 已加载的净牌图像（`office-frontdesk-sign-clean.png`）
 * @param projectName 项目显示名（空/未选项目时回退为品牌名 HiveWeave）
 */
export function renderFrontSign(
  baseImage: HTMLImageElement | HTMLCanvasElement,
  projectName: string | null | undefined,
): HTMLCanvasElement {
  const { w, h } = FRONT_SIGN_CANVAS;
  const out = document.createElement("canvas");
  out.width = w;
  out.height = h;
  const ctx = out.getContext("2d");
  if (!ctx) return out;

  // ① 先铺净牌（含木柜/台面/描边；牌面已是纯蓝）
  ctx.drawImage(baseImage, 0, 0, w, h);

  const text = (projectName ?? "").trim() || "HiveWeave";
  const { x0, x1 } = FRONT_SIGN;
  const flatW = x1 - x0;
  // 展平高度取牌高中位数
  const hs: number[] = [];
  for (let x = x0; x <= x1; x++) hs.push(frontSignHeight(x));
  hs.sort((a, b) => a - b);
  const flatH = Math.max(8, Math.round(hs[Math.floor(hs.length / 2)]));

  // ② 展平坐标系里画水平文字
  const flat = document.createElement("canvas");
  flat.width = flatW;
  flat.height = flatH;
  const fctx = flat.getContext("2d");
  if (!fctx) return out;

  // 字号自适应：占满牌宽的 TEXT_WIDTH_RATIO
  let size = flatH - 2;
  const fit = () => {
    fctx.font = `700 ${size}px ${FONT_FAMILY}`;
    return fctx.measureText(text).width;
  };
  let tw = fit();
  const maxW = flatW * TEXT_WIDTH_RATIO;
  while (size > 6 && tw > maxW) {
    size -= 1;
    tw = fit();
  }
  fctx.font = `700 ${size}px ${FONT_FAMILY}`;
  fctx.textAlign = "center";
  fctx.textBaseline = "middle";
  // 白字 + 深蓝描边（贴近原牌 KAIROSOFT 的徽标观感）
  fctx.lineJoin = "round";
  fctx.lineWidth = Math.max(2, Math.round(size * 0.11));
  fctx.strokeStyle = "#0f2f8c";
  fctx.strokeText(text, flatW / 2, flatH / 2);
  fctx.fillStyle = "#ffffff";
  fctx.fillText(text, flatW / 2, flatH / 2);

  // ③ 逐列卷回弧面
  //    ⚠ 必须逐列单独 drawImage（源 1px 宽 → 目标 1px 宽、可变高），
  //      不能用一次整体变换：弧面上下边缘曲率不同，整体变换拟合不出。
  for (let x = x0; x <= x1; x++) {
    const ty = frontSignTop(x);
    const hh = frontSignHeight(x);
    if (hh <= 0) continue;
    const sx = x - x0; // 展平坐标系里的列
    ctx.drawImage(
      flat,
      sx, 0, 1, flatH,          // 源：展平图该列整高
      x, ty, 1, hh,             // 目标：弧面该列的实际高
    );
  }

  return out;
}
