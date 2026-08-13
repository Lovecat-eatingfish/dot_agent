import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发：vite dev server 5173，/api 代理到后端 8000
// 生产：vite build → dist/，由 FastAPI StaticFiles 托管在同源 8000
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
