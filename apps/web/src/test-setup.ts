import '@testing-library/jest-dom/vitest'

// Animate UI / motion 的 useIsInView 无条件 new IntersectionObserver()，
// jsdom 没有该 API —— 任何挂载 TokenUsagePanel / WorkLogPanel(Slide) /
// CountingNumber 的测试都会在 effect 里 ReferenceError。最小桩：只满足
// 构造与观察方法（测试不依赖真实交叉状态；motion inView=false 路径下
// isInView 恒真，桩只需让构造不炸）。
if (typeof globalThis.IntersectionObserver === 'undefined') {
  class IntersectionObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  globalThis.IntersectionObserver =
    IntersectionObserverStub as unknown as typeof IntersectionObserver;
}
