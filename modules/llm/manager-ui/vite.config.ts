import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served at llm-manager.<domain>/ behind Caddy + Authentik SSO.
// In `vite dev` we proxy /api + /v1 to a local manager for convenience.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8091", changeOrigin: true },
      "/v1": { target: "http://127.0.0.1:8091", changeOrigin: true },
    },
  },
});
