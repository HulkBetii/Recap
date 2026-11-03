import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const backendTarget = process.env.VITE_RECAP_API_TARGET ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../recap_ui/static",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
        configure(proxy) {
          proxy.on("proxyReq", (proxyRequest) => {
            proxyRequest.setHeader("origin", backendTarget);
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
    exclude: ["e2e/**", "node_modules/**"],
    // Multi-step userEvent flows take ~4-5 s when all files run in parallel; the 5 s default flakes.
    testTimeout: 15000,
  },
});
