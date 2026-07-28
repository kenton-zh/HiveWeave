/**
 * Minimal HiveWeave H5 game test seam (contract v1.0).
 * Copy into the game project; wire to real update/render.
 * Spec: docs/spec/h5-game-test-harness.md
 */

export type HwButtons =
  | "up"
  | "down"
  | "left"
  | "right"
  | "space"
  | "enter"
  | "escape"
  | "w"
  | "a"
  | "s"
  | "d"
  | "left_mouse_button"
  | "right_mouse_button";

export type HwStep = {
  buttons: HwButtons[];
  frames: number;
  mouse_x?: number;
  mouse_y?: number;
};

export type HwCase = {
  id: string;
  setup: () => void;
  drive: HwStep[];
  assertCode: () => boolean;
  visionCriteria: string;
  screenshotHint?: string;
};

export type HwCaseResult = {
  id: string;
  codePass: boolean;
  codeErrors: string[];
  visionCriteria: string;
  screenshotHint?: string;
  simulatedMs: number;
};

declare global {
  interface Window {
    __HW_TEST__?: {
      version: "1.0";
      ready: boolean;
      list: () => string[];
      run: (caseId: string) => Promise<HwCaseResult>;
      getState: () => string;
      pause?: () => void;
      resume?: () => void;
      step?: (frames?: number) => void;
      setSpeed?: (rate: number) => void;
    };
    render_game_to_text?: () => string;
    advanceTime?: (ms: number) => void | Promise<void>;
  }
}

/** Example wiring — replace stubs with your game loop. */
export function installHwTest(opts: {
  cases: HwCase[];
  getState: () => object;
  /** Apply button set for one frame, then call update(1/60)+render */
  applyFrame: (step: HwStep) => void;
  enabled?: boolean;
}): void {
  if (opts.enabled === false) return;

  const byId = new Map(opts.cases.map((c) => [c.id, c]));

  const getStateJson = () => JSON.stringify(opts.getState());

  window.render_game_to_text = getStateJson;

  window.advanceTime = (ms: number) => {
    const frames = Math.max(1, Math.round(ms / (1000 / 60)));
    for (let i = 0; i < frames; i++) {
      opts.applyFrame({ buttons: [], frames: 1 });
    }
  };

  window.__HW_TEST__ = {
    version: "1.0",
    ready: true,
    list: () => opts.cases.map((c) => c.id),
    getState: getStateJson,
    async run(caseId: string): Promise<HwCaseResult> {
      const c = byId.get(caseId);
      if (!c) {
        return {
          id: caseId,
          codePass: false,
          codeErrors: [`unknown case: ${caseId}`],
          visionCriteria: "",
          simulatedMs: 0,
        };
      }
      c.setup();
      let frames = 0;
      for (const step of c.drive) {
        for (let i = 0; i < step.frames; i++) {
          opts.applyFrame(step);
          frames += 1;
        }
      }
      let codePass = false;
      const codeErrors: string[] = [];
      try {
        codePass = !!c.assertCode();
        if (!codePass) codeErrors.push("assertCode returned false");
      } catch (e) {
        codeErrors.push(e instanceof Error ? e.message : String(e));
      }
      return {
        id: c.id,
        codePass,
        codeErrors,
        visionCriteria: c.visionCriteria,
        screenshotHint: c.screenshotHint ?? "canvas",
        simulatedMs: Math.round((frames * 1000) / 60),
      };
    },
  };
}
