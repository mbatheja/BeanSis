import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiPort = process.env.KIDNEY_API_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  // Only used by the dev server (`npm run dev:local`): forwards /api/* to the Python API on this computer.
  server: {
    proxy: {
      "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
});
