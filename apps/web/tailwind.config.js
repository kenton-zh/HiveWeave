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
      },
    },
  },
  plugins: [],
};
