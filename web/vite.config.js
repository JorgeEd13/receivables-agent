import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the UI runs on the Vite server and proxies /api to the FastAPI
// service, so the browser talks to a single origin (no CORS). In production the
// built assets are served by FastAPI itself, so /api is same-origin there too.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
