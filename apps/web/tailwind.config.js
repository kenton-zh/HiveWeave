export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter", "ui-sans-serif", "system-ui", "-apple-system",
          "PingFang SC", "Microsoft YaHei", "Noto Sans SC", "sans-serif",
        ],
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "monospace"],
      },
      colors: {
        g: {
          // Cool-neutral surfaces (Linear-style console)
          bg: "#ffffff",
          "bg-soft": "#f6f7f9",
          "bg-muted": "#eceef3",
          border: "#e4e6ed",
          "border-strong": "#d3d7e1",
          "border-focus": "#4f46e5",
          fg: "#181b23",
          "fg-2": "#404654",
          "fg-3": "#6d7482",
          "fg-4": "#9ba1ae",
          // Brand: refined indigo
          blue: "#4f46e5",
          "blue-bg": "#eceefb",
          // Semantic
          red: "#e5484d",
          "red-bg": "#fdecec",
          green: "#189a52",
          "green-bg": "#e4f5eb",
          yellow: "#c77400",
          "yellow-bg": "#fbf0dc",
          // P0 新增：亮色档（状态点 / 图标在深底或紧凑行的可读性）
          // 与 purple（合并中 / 特殊状态语义）
          "red-vivid": "#f87171",
          "green-vivid": "#34d399",
          "yellow-vivid": "#fbbf24",
          "blue-vivid": "#818cf8",
          purple: "#7c3aed",
          "purple-bg": "#f3e8ff",
          "purple-vivid": "#a78bfa",
          // ── 游戏窗口层（与办公室等距场景同风格）──────────────────────
          // 取值自 docs/design-refs/office-main-ui-ref-v1.png（3daistudio
          // edit_image_gpt 以 office-scene-bg.png 为参考图生成，2026-09-22）。
          // ⚠ 这里不是像素皮肤 —— 场景是等距高清插画，见设计规格 §3 的美术路线裁决。
          // FE-11 分层基调（设计规格 §4.3 · 2026-09-24 B3）：场景邻近区（窗口
          // chrome）取暖色系，与木地板/暖木家具同调；工作面板保持冷中性（上方
          // g-bg/border/fg 等中性令牌 ⛔ 不动）。
          "win-header": "#453e37",
          "win-header-fg": "#f2f5f9",
          "win-header-hover": "#38322c",
          "win-body": "rgba(246,242,236,.97)",
          // FE-11：文本密集窗（chat/详情/日志）正文高不透明 —— 地板家具不透出
          "win-body-solid": "#f6f2ec",
          "win-border": "rgba(56,44,34,.12)",
          // FE-11：聚焦窗轻边框（不做大面积发光，§4.5）
          "win-border-focus": "rgba(74,60,48,.38)",
          // ⚠ 红色关闭钮已按 §4.5 中性化（中性描边 + 悬停底色，见 gamewindow.css）；
          //   这两个令牌保留给「危险操作关闭」（如丢弃确认），当前窗口 chrome 不使用
          "win-close": "#d24d44",
          "win-close-hover": "#c03d34",
          "win-control": "#c9c0b6",
          "win-control-hover": "rgba(255,255,255,.12)",
          "win-scroll": "rgba(74,60,48,.26)",
          "win-scroll-hover": "rgba(74,60,48,.42)",
          // FE-11：HUD 底盘令牌（替代 OfficeWorkspace.tsx 的裸值 bg-[#10131a]/78）
          "hud": "rgba(26,21,18,.78)",
        },
      },
      borderRadius: {
        gm: "8px",
        gmLg: "12px",
        // FE-11（设计规格 §4.1 v2.12）：小标签/徽章/轻提示角标的第三档，
        // 与既有 rounded-[6px] 用例一致，避免再开 4px 第四档
        gmSm: "6px",
      },
      // FE-11 间距体系（设计规格 §4.1 v2.12 · A1）：统一阶梯 4/8/12/16/24/32px
      // —— 恰为 Tailwind 默认 spacing 档 p-1/p-2/p-3/p-4/p-6/p-8，落地零新令牌；
      // 新代码不得开中间值（5/7/10/18 等），先用留白与对齐划分信息再加卡片。
      transitionDuration: {
        // FE-20 动效时长令牌（设计规格 §4.9 · 2026-09-24 R2 按组件分档，
        // 行为区间转为上限；用法 duration-gm-btn 等）
        "gm-btn": "120ms", // 按钮 hover/按下（档 100–150ms）
        "gm-panel": "180ms", // 面板打开/关闭（档 160–220ms）
        "gm-tab": "150ms", // 标签页切换 / 成员选中（档 120–180ms）
        "gm-move": "300ms", // 位移动：走位/滑入类（档 250–350ms）
      },
      boxShadow: {
        gm: "0 1px 2px 0 rgba(23,25,35,.06), 0 1px 3px 0 rgba(23,25,35,.05)",
        "gm-sm": "0 1px 2px 0 rgba(23,25,35,.05)",
        "gm-md": "0 2px 4px -1px rgba(23,25,35,.06), 0 4px 10px -2px rgba(23,25,35,.08)",
        "gm-lg": "0 4px 8px -2px rgba(23,25,35,.06), 0 12px 28px -6px rgba(23,25,35,.12)",
        "gm-pop": "0 6px 12px -2px rgba(23,25,35,.08), 0 20px 44px -10px rgba(23,25,35,.18)",
        "gm-glow": "0 0 0 1px rgba(79,70,229,.16), 0 4px 16px 2px rgba(79,70,229,.20)",
        // 游戏窗口：大范围柔阴影（**无硬边**），与等距场景的柔光感一致
        "gm-window": "0 18px 40px -12px rgba(16,24,40,.34), 0 4px 12px -4px rgba(16,24,40,.16)",
      },
      transitionTimingFunction: {
        "gm-out": "cubic-bezier(0.22, 1, 0.36, 1)",
        "gm-spring": "cubic-bezier(0.34, 1.4, 0.44, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-down": {
          from: { opacity: "0", transform: "translateY(-6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in-right": {
          from: { opacity: "0", transform: "translateX(24px) scale(0.97)" },
          to: { opacity: "1", transform: "translateX(0) scale(1)" },
        },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.96)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
        "ping-ring": {
          "0%": { transform: "scale(1)", opacity: "0.6" },
          "80%, 100%": { transform: "scale(2.1)", opacity: "0" },
        },
        shimmer: {
          from: { backgroundPosition: "200% 0" },
          to: { backgroundPosition: "-200% 0" },
        },
      },
      animation: {
        "fade-in": "fade-in 0.25s cubic-bezier(0.22, 1, 0.36, 1) both",
        "slide-up": "slide-up 0.3s cubic-bezier(0.22, 1, 0.36, 1) both",
        "slide-down": "slide-down 0.22s cubic-bezier(0.22, 1, 0.36, 1) both",
        "slide-in-right": "slide-in-right 0.28s cubic-bezier(0.34, 1.3, 0.44, 1) both",
        "scale-in": "scale-in 0.18s cubic-bezier(0.22, 1, 0.36, 1) both",
        "pulse-soft": "pulse-soft 2.4s ease-in-out infinite",
        "ping-ring": "ping-ring 1.8s cubic-bezier(0, 0, 0.2, 1) infinite",
        shimmer: "shimmer 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
