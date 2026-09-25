/**
 * usePanZoom 缩放语义与交互（FE-10 / SR-02，方案 §10.4，验收 T-16）。
 *
 * 锁死四条最容易回退的契约：
 *  1. factor 是放大倍率：zoomBy(>1) ⇒ 可见时间跨度变小（放大）；
 *     旧实现「factor 乘跨度」导致「+」按钮实际在缩小 —— 回归即失败；
 *  2. zoomBy 以时间区中心为锚（不含成员标签列），锚点时刻不动；
 *  3. 滚轮以光标下时间为锚（ctrl/横向滚轮才缩放）；
 *  4. 真拖动（位移 > slop）结束后的 click 不下发给任务段按钮。
 */
import { fireEvent, render, screen, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRef } from "react";
import {
  isLiveWindow,
  usePanZoom,
  ZOOM_STEP_IN,
  ZOOM_STEP_OUT,
} from "./usePanZoom";
import type { PanZoomApi, TimeViewport } from "./usePanZoom";

const SINCE = 1_000_000;
const UNTIL = 2_000_000; // 跨度 1e6 ms

function Harness({
  apiRef,
  labelWidth = 100,
  initial = { since: SINCE, until: UNTIL },
}: {
  apiRef: { current: PanZoomApi | null };
  labelWidth?: number;
  initial?: TimeViewport;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const pan = usePanZoom({ containerRef: ref, initial, labelWidth });
  apiRef.current = pan;
  return (
    <div>
      <div ref={ref} {...pan.bind} data-testid="container">
        <button data-interactive data-testid="seg" onClick={() => segClicks.push(1)}>
          任务条
        </button>
      </div>
      <span data-testid="since">{Math.round(pan.view.since)}</span>
      <span data-testid="until">{Math.round(pan.view.until)}</span>
      <span data-testid="span">{Math.round(pan.view.until - pan.view.since)}</span>
    </div>
  );
}

const segClicks: number[] = [];

function stubRect(el: HTMLElement, left = 100, width = 900) {
  vi.spyOn(el, "getBoundingClientRect").mockReturnValue({
    left,
    top: 0,
    right: left + width,
    bottom: 500,
    width,
    height: 500,
    x: left,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
  // jsdom 的 clientWidth 恒为 0，拖拽换算按时间区宽 = width - labelWidth
  Object.defineProperty(el, "clientWidth", { value: width, configurable: true });
}

function renderHarness(labelWidth = 100) {
  const apiRef: { current: PanZoomApi | null } = { current: null };
  render(<Harness apiRef={apiRef} labelWidth={labelWidth} />);
  const el = screen.getByTestId("container");
  stubRect(el, 100, 900);
  return { apiRef, el };
}

/** jsdom 没有指针捕获 API；jsdom 25 也没有 PointerEvent 构造器
 *  （fireEvent 回退 Event 会丢 clientX → 平移算出 NaN）。用 MouseEvent
 *  补位（其 init 支持 clientX/button，pointerId 由捕获桩消化）。 */
beforeEach(() => {
  segClicks.length = 0;
  (Element.prototype as any).setPointerCapture ??= vi.fn();
  (Element.prototype as any).releasePointerCapture ??= vi.fn();
  if (typeof (window as any).PointerEvent !== "function") {
    (window as any).PointerEvent = MouseEvent;
  }
});

describe("usePanZoom 缩放方向（SR-02 / T-16）", () => {
  it("zoomBy(放大倍率>1)：可见跨度变小（span/1.25），时间区中心锚点时刻不动", () => {
    const { apiRef } = renderHarness();
    // 时间区 = [left+labelWidth, left+width] = [200, 1000]，中心 clientX=600
    // ratio = (600-100-100)/800 = 0.5 → anchorTs = 1.5e6
    act(() => apiRef.current!.zoomBy(ZOOM_STEP_IN));
    expect(Number(screen.getByTestId("span").textContent)).toBeCloseTo(800_000, 0);
    expect(Number(screen.getByTestId("since").textContent)).toBeCloseTo(1_100_000, 0);
    expect(Number(screen.getByTestId("until").textContent)).toBeCloseTo(1_900_000, 0);
  });

  it("zoomBy(<1)：可见跨度变大（缩小），中心锚点不动", () => {
    const { apiRef } = renderHarness();
    act(() => apiRef.current!.zoomBy(ZOOM_STEP_OUT));
    expect(Number(screen.getByTestId("span").textContent)).toBeCloseTo(1_250_000, 0);
    expect(Number(screen.getByTestId("since").textContent)).toBeCloseTo(875_000, 0);
    expect(Number(screen.getByTestId("until").textContent)).toBeCloseTo(2_125_000, 0);
  });

  it("放大到最小跨度（5min）后钳制，锚点仍不动", () => {
    const { apiRef } = renderHarness();
    act(() => apiRef.current!.zoomBy(1 / (300_000 / 1_000_000) * 0.999)); // newSpan≈300_300 > MIN
    act(() => apiRef.current!.zoomBy(ZOOM_STEP_IN * 100)); // 远超最小值
    const span = Number(screen.getByTestId("span").textContent);
    expect(span).toBe(300_000);
    // 锚点 ratio=0.5 → since = anchor - span/2
    expect(Number(screen.getByTestId("since").textContent)).toBeCloseTo(1_500_000 - 150_000, 0);
  });

  it("zoomAt：光标下时刻在缩放前后保持同一时间区占比（滚轮锚点契约）", () => {
    const { apiRef } = renderHarness();
    // clientX=300 → ratio=(300-200)/800=0.125，anchorTs=1.125e6
    act(() => apiRef.current!.zoomAt(300, ZOOM_STEP_IN));
    const since = Number(screen.getByTestId("since").textContent);
    const span = Number(screen.getByTestId("span").textContent);
    const ratioAfter = (1_125_000 - since) / span;
    expect(ratioAfter).toBeCloseTo(0.125, 5);
  });
});

describe("usePanZoom 滚轮缩放（光标锚点）", () => {
  it("ctrl+上滚（deltaY<0）= 放大：跨度变小，光标时刻不动", () => {
    const { el } = renderHarness();
    act(() => {
      fireEvent.wheel(el, { deltaY: -100, deltaX: 0, ctrlKey: true, clientX: 300 });
    });
    const span = Number(screen.getByTestId("span").textContent);
    expect(span).toBeCloseTo(1_000_000 / 1.12, 0);
    const since = Number(screen.getByTestId("since").textContent);
    expect((1_125_000 - since) / span).toBeCloseTo(0.125, 5);
  });

  it("下滚（deltaY>0）= 缩小：跨度变大", () => {
    const { el } = renderHarness();
    act(() => {
      fireEvent.wheel(el, { deltaY: 100, deltaX: 0, ctrlKey: true, clientX: 300 });
    });
    expect(Number(screen.getByTestId("span").textContent)).toBeCloseTo(1_000_000 * 1.12, 0);
  });

  it("无 ctrl 的垂直滚轮不缩放（交给原生纵向滚动）", () => {
    const { el } = renderHarness();
    act(() => {
      fireEvent.wheel(el, { deltaY: -100, deltaX: 0, clientX: 300 });
    });
    expect(Number(screen.getByTestId("span").textContent)).toBe(1_000_000);
  });
});

describe("usePanZoom 拖动与点击区分（§10.4：拖动结束不误开任务）", () => {
  it("真拖动（位移>slop）结束后的 click 不下发任务段按钮", () => {
    const { el } = renderHarness();
    const seg = screen.getByTestId("seg");
    act(() => {
      fireEvent.pointerDown(el, { clientX: 500, button: 0 });
      fireEvent.pointerMove(el, { clientX: 520 });
      fireEvent.pointerUp(el, { clientX: 520 });
    });
    fireEvent.click(seg);
    expect(segClicks).toHaveLength(0);
    // 拖动本身生效：窗口平移（520-500)/800*1e6 = 25000ms
    expect(Number(screen.getByTestId("since").textContent)).toBeCloseTo(SINCE - 25_000, 0);
  });

  it("未超过 slop 的微动不算拖动，click 正常打开任务", () => {
    const { el } = renderHarness();
    const seg = screen.getByTestId("seg");
    act(() => {
      fireEvent.pointerDown(el, { clientX: 500, button: 0 });
      fireEvent.pointerMove(el, { clientX: 501 });
      fireEvent.pointerUp(el, { clientX: 501 });
    });
    fireEvent.click(seg);
    expect(segClicks).toHaveLength(1);
  });

  it("直接点击（无拖动）正常打开任务", () => {
    const { el } = renderHarness();
    fireEvent.click(screen.getByTestId("seg"));
    expect(segClicks).toHaveLength(1);
    expect(el).toBeTruthy();
  });

  it("pointercancel 复位拖拽态，后续 click 不被抑制", () => {
    const { el } = renderHarness();
    const seg = screen.getByTestId("seg");
    act(() => {
      fireEvent.pointerDown(el, { clientX: 500, button: 0 });
      fireEvent.pointerMove(el, { clientX: 560 });
      fireEvent.pointerCancel(el);
    });
    fireEvent.click(seg);
    expect(segClicks).toHaveLength(1);
  });
});

describe("usePanZoom 其它契约", () => {
  it("setView 钳制跨度到 [5min, 45d]", () => {
    const { apiRef } = renderHarness();
    act(() => apiRef.current!.setView({ since: 0, until: 100 })); // 过小
    expect(Number(screen.getByTestId("span").textContent)).toBe(300_000);
    act(() =>
      apiRef.current!.setView({ since: 0, until: 100 * 24 * 3600e3 }), // 过大
    );
    expect(Number(screen.getByTestId("span").textContent)).toBe(45 * 24 * 3600e3);
  });

  it("jumpToNow 以 now 为终点平移，跨度不变", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(5_000_000);
      const apiRef: { current: PanZoomApi | null } = { current: null };
      render(<Harness apiRef={apiRef} labelWidth={0} />);
      act(() => apiRef.current!.jumpToNow());
      expect(Number(screen.getByTestId("until").textContent)).toBe(5_000_000);
      expect(Number(screen.getByTestId("span").textContent)).toBe(1_000_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it("isLiveWindow：终点距 now < 1min 视为 live", () => {
    const now = 10_000_000;
    expect(isLiveWindow({ since: now - 3600e3, until: now }, now)).toBe(true);
    expect(isLiveWindow({ since: now - 3600e3, until: now - 61e3 }, now)).toBe(false);
  });
});
