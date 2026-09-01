import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  // Served from FastAPI under /app/, so asset URLs need that prefix.
  base: "/app/",
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  server: {
    proxy: {
      // Dev-mode only: forward all API + UI legacy routes to the FastAPI server.
      "/auth":     "http://localhost:8000",
      "/reports":  "http://localhost:8000",
      "/scans":    "http://localhost:8000",
      "/runs":     "http://localhost:8000",
      "/projects": "http://localhost:8000",
      "/agents":   "http://localhost:8000",
      "/chat":     "http://localhost:8000",
      "/ui":       "http://localhost:8000",
      "/healthz":  "http://localhost:8000",
      "/static":   "http://localhost:8000",
    },
  },
});
