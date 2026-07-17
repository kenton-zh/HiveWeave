export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["DM Sans", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
      colors: {
        g: {
          bg: "#ffffff",
          "bg-soft": "#f9f9fa",
          "bg-muted": "#eff1f4",
          border: "#ebebeb",
          "border-strong": "#dadce0",
          "border-focus": "#4285f4",
          fg: "#0e1115",
          "fg-2": "#333942",
          "fg-3": "#7f8d9f",
          "fg-4": "#9aa0a6",
          blue: "#4285f4",
          "blue-bg": "#dbeafe",
          red: "#ea4335",
          "red-bg": "#fce8e6",
          green: "#34a853",
          "green-bg": "#e6f4ea",
          yellow: "#fbbc05",
          "yellow-bg": "#fef7e0",
        },
      },
      borderRadius: {
        gm: "8px",
        gmLg: "12px",
      },
      boxShadow: {
        gm: "0 1px 2px 0 rgba(60,64,67,.30), 0 1px 3px 1px rgba(60,64,67,.15)",
        "gm-sm": "0 1px 1px 0 rgba(60,64,67,.20)",
        "gm-md": "0 2px 6px 2px rgba(60,64,67,.15), 0 1px 2px 0 rgba(60,64,67,.30)",
        "gm-lg": "0 4px 12px 4px rgba(60,64,67,.10), 0 2px 4px 0 rgba(60,64,67,.20)",
        "gm-pop": "0 8px 24px 6px rgba(60,64,67,.12), 0 2px 6px 0 rgba(60,64,67,.18)",
        "gm-glow": "0 0 0 1px rgba(66,133,244,.18), 0 4px 16px 2px rgba(66,133,244,.22)",
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
