import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:4000",
    },
  },
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "zustand",
      "phoenix",
      "@xyflow/react",
      "pixi.js",
    ],
  },
});
