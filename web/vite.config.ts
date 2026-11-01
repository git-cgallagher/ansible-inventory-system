import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands in server/ais/static, where FastAPI serves it at / (same origin as
// the API, so the bearer token needs no CORS). `npm run dev` proxies the API to a local
// `ais serve` on :8000.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../server/ais/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
});
