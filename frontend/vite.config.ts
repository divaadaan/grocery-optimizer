import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Port 3000 matches the backend's default CORS allowlist; the proxy makes
// CORS moot in dev by serving the API from the same origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
