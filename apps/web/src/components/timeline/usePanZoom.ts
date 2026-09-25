/**
 * usePanZoom — 时间轴视口交互 hook（Timeline v4 §5.2）。
 *
 * 从 OrgTree.tsx:840-981 的内联 pan/zoom 重写而来，但视口模型不同：
 * OrgTree 是 2D 自由变换（tx/ty/scale），时间轴用「时间窗口」
 * {since, until} 作单一事实源 —— 段定位、TimeAxis、MiniMap、深链
 * 全部直接消费它，缩放/平移只改窗口，内容按窗口重算百分比，
 * 文字永不形变。
 *
 * 几何注意：泳道左侧有固定标签列（labelWidth），段百分比定位在
 * 「时间区」（容器宽 − labelWidth）内。zoomAt/拖拽的换算必须排除
 * 标签列，否则锚点漂移、拖拽跟随速度错误。
 *
 * 缩放语义（FE-10 / SR-02，方案 §10.4）：zoomAt/zoomBy 的 factor 是
 * **放大倍率** —— factor > 1 = 放大（同一任务条占更大屏宽，可见时间
 * 跨度变为原来的 1/factor），factor < 1 = 缩小（跨度变大）。
 * 内部换算 newSpan = span / factor，历史遗留的「factor 乘跨度」口径
 * 已废弃（旧口径下「+」按钮实际在缩小画面，方向相反）。
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface TimeViewport {
  /** 现实毫秒 */
  since: number;
  /** 现实毫秒 */
  until: number;
}

export interface PanZoomApi {
  view: TimeViewport;
  setView: (v: TimeViewport) => void;
  /**
   * 以某客户端 X 坐标为锚点缩放（wheel 用）：保持锚点下的时刻不动。
   * factor 为放大倍率（>1 放大 / <1 缩小，见文件头注释）。
   */
  zoomAt: (clientX: number, factor: number) => void;
  /** 缩放（以时间区中心为锚，不含成员标签列）。factor 同上。 */
  zoomBy: (factor: number) => void;
  /** 平移/缩放后窗口终点贴近 now 时吸附（保持 live 语义） */
  jumpToNow: () => void;
  isDragging: boolean;
  bind: {
    onPointerDown: (e: React.PointerEvent) => void;
    onPointerMove: (e: React.PointerEvent) => void;
    onPointerUp: (e: React.PointerEvent) => void;
    onPointerCancel: (e: React.PointerEvent) => void;
    /** 捕获段点击抑制：拖动结束后的 click 不下发给任务段（防误开任务） */
    onClickCapture: (e: React.MouseEvent) => void;
  };
}

const MIN_SPAN_MS = 5 * 60 * 1000; // 最小 5 分钟
const MAX_SPAN_MS = 45 * 24 * 3600 * 1000; // 最大 45 天
const SNAP_NOW_MS = 60 * 1000; // until 距 now 小于 1 分钟 → 视为 live

/** 按钮/滚轮统一步进（放大倍率口径）。缩小 = 1/放大，保证往返对称。 */
export const ZOOM_STEP_IN = 1.25;
export const ZOOM_STEP_OUT = 1 / ZOOM_STEP_IN;
export const WHEEL_STEP_IN = 1.12;
export const WHEEL_STEP_OUT = 1 / WHEEL_STEP_IN;
/** 拖动超过该位移（px）才算真拖动，结束后的 click 被抑制 */
const DRAG_CLICK_SLOP_PX = 3;

export function clampSpan(spanMs: number): number {
  return Math.max(MIN_SPAN_MS, Math.min(MAX_SPAN_MS, spanMs));
}

export function usePanZoom(opts: {
  containerRef: React.RefObject<HTMLElement | null>;
  initial: TimeViewport;
  /** 深链等外部来源覆写窗口时调用（内部不做任何持久化） */
  nowMs?: () => number;
  /** 左侧标签列宽度（px）。时间区 = 容器宽 − labelWidth，默认 0 */
  labelWidth?: number;
  /**
   * 容器 DOM 是否已挂载。骨架屏阶段容器不存在，wheel effect 会提前返回；
   * 该标志变化时重跑 effect 补挂监听（否则缩放永久失效）。
   */
  containerReady?: boolean;
}): PanZoomApi {
  const { containerRef, initial } = opts;
  const now = opts.nowMs ?? (() => Date.now());
  const labelWidth = opts.labelWidth ?? 0;
  const containerReady = opts.containerReady ?? true;

  const [view, setViewRaw] = useState<TimeViewport>(initial);
  const [isDragging, setIsDragging] = useState(false);
  const dragRef = useRef<{
    startX: number;
    since: number;
    until: number;
    /** 位移超过 slop ⇒ 真拖动，pointerup 后的 click 会被抑制 */
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);

  const setView = useCallback((v: TimeViewport) => {
    const span = clampSpan(v.until - v.since);
    setViewRaw({ since: v.since, until: v.since + span });
  }, []);

  /** clientX → 锚点缩放：保持光标下的时刻不动（仅时间区参与换算）。
   *  factor 为放大倍率：newSpan = span / factor（>1 放大 ⇒ 跨度变小）。 */
  const zoomAt = useCallback(
    (clientX: number, factor: number) => {
      const el = containerRef.current;
      // factor <= 0 / 非有限坐标直接拒绝：NaN 一旦进 view 会毒化全部换算
      if (!el || !(factor > 0) || !Number.isFinite(clientX)) return;
      const rect = el.getBoundingClientRect();
      const laneW = Math.max(1, rect.width - labelWidth);
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left - labelWidth) / laneW));
      setViewRaw((prev) => {
        const span = prev.until - prev.since;
        const anchorTs = prev.since + span * ratio;
        const newSpan = clampSpan(span / factor);
        return {
          since: anchorTs - newSpan * ratio,
          until: anchorTs + newSpan * (1 - ratio),
        };
      });
    },
    [containerRef, labelWidth],
  );

  const zoomBy = useCallback(
    (factor: number) => {
      const el = containerRef.current;
      const rect = el?.getBoundingClientRect();
      // 时间区中心（排除标签列）
      const cx = rect ? rect.left + labelWidth + (rect.width - labelWidth) / 2 : 0;
      zoomAt(cx, factor);
    },
    [containerRef, labelWidth, zoomAt],
  );

  const jumpToNow = useCallback(() => {
    setViewRaw((prev) => {
      const span = prev.until - prev.since;
      const n = now();
      return { since: n - span, until: n };
    });
  }, [now]);

  // Wheel 缩放（non-passive 才能 preventDefault）— 与 OrgTree 同款挂法。
  // containerReady 入依赖：首次渲染若处于骨架屏（容器未挂载），数据到达后补挂。
  // 滚轮以光标下时间为锚点（zoomAt）；向下滚 = 缩小（跨度变大）。
  useEffect(() => {
    const el = containerRef.current;
    if (!el || !containerReady) return;
    const onWheel = (e: WheelEvent) => {
      // 垂直滚轮交给原生纵向滚动（泳道多时），只有 ctrl/横向才缩放
      if (!e.ctrlKey && Math.abs(e.deltaX) < Math.abs(e.deltaY)) return;
      e.preventDefault();
      zoomAt(e.clientX, e.deltaY > 0 ? WHEEL_STEP_OUT : WHEEL_STEP_IN);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [containerRef, zoomAt, containerReady]);

  // 指针拖拽平移（只取 X；纵向交给原生滚动）
  const onPointerDown = useCallback((e: React.PointerEvent) => {
    suppressClickRef.current = false; // 新按下作废上一轮遗留的抑制
    if ((e.target as HTMLElement).closest("[data-interactive]")) return;
    dragRef.current = {
      startX: e.clientX,
      since: view.since,
      until: view.until,
      moved: false,
    };
    setIsDragging(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }, [view.since, view.until]);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current;
    const el = containerRef.current;
    if (!d || !el) return;
    if (Math.abs(e.clientX - d.startX) > DRAG_CLICK_SLOP_PX) d.moved = true;
    const width = Math.max(1, el.clientWidth - labelWidth); // 时间区宽度
    const span = d.until - d.since;
    const deltaMs = ((e.clientX - d.startX) / width) * span;
    setViewRaw({ since: d.since - deltaMs, until: d.until - deltaMs });
  }, [containerRef, labelWidth]);

  const endDrag = useCallback(() => {
    // 真拖动结束后抑制紧随的 click（§10.4：拖动结束不误开任务）；
    // click 不来的场景由下一次 pointerdown 复位兜底。
    if (dragRef.current?.moved) suppressClickRef.current = true;
    dragRef.current = null;
    setIsDragging(false);
  }, []);

  const onPointerUp = useCallback(() => {
    endDrag();
    // live 吸附：终点本来在 now 附近，平移后也贴回 now（保持"正在发生"语义）
    setViewRaw((prev) => {
      const n = now();
      if (Math.abs(prev.until - n) < SNAP_NOW_MS) {
        const span = prev.until - prev.since;
        return { since: n - span, until: n };
      }
      return prev;
    });
  }, [endDrag, now]);

  // pointer cancel 不做吸附，只复位拖拽态（否则 dragRef 卡死 → 无按键也平移）
  const onPointerCancel = useCallback(() => {
    endDrag(); // 先清拖拽态（其间会把 suppress 置 true）
    suppressClickRef.current = false; // cancel 后没有 click，别让抑制漏到下次点击
  }, [endDrag]);

  // 捕获段拦截：刚结束真拖动的那次 click 不下发给任务段按钮
  const onClickCapture = useCallback((e: React.MouseEvent) => {
    if (!suppressClickRef.current) return;
    e.preventDefault();
    e.stopPropagation();
    suppressClickRef.current = false;
  }, []);

  return {
    view,
    setView,
    zoomAt,
    zoomBy,
    jumpToNow,
    isDragging,
    bind: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onClickCapture },
  };
}

/** 窗口是否 live（终点贴着当前时刻）。 */
export function isLiveWindow(view: TimeViewport, nowMs: number): boolean {
  return view.until >= nowMs - SNAP_NOW_MS;
}
