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
        },
      },
      borderRadius: {
        gm: "8px",
        gmLg: "12px",
      },
      boxShadow: {
        gm: "0 1px 2px 0 rgba(23,25,35,.06), 0 1px 3px 0 rgba(23,25,35,.05)",
        "gm-sm": "0 1px 2px 0 rgba(23,25,35,.05)",
        "gm-md": "0 2px 4px -1px rgba(23,25,35,.06), 0 4px 10px -2px rgba(23,25,35,.08)",
        "gm-lg": "0 4px 8px -2px rgba(23,25,35,.06), 0 12px 28px -6px rgba(23,25,35,.12)",
        "gm-pop": "0 6px 12px -2px rgba(23,25,35,.08), 0 20px 44px -10px rgba(23,25,35,.18)",
        "gm-glow": "0 0 0 1px rgba(79,70,229,.16), 0 4px 16px 2px rgba(79,70,229,.20)",
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
