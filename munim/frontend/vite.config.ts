import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Backend (FastAPI) serves frontend/dist at "/" in production; in dev we proxy /api to it.
const api = { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy: api },
  preview: { port: 4173, proxy: api },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
});
