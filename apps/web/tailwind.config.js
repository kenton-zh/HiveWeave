export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: { DEFAULT: "#0f1117", card: "#1a1d27", border: "#2a2d3a" },
        accent: { DEFAULT: "#6c8cff", dim: "#4a6ae0" },
      },
    },
  },
  plugins: [],
};
