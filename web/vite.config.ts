import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时前端跑在 5173，后端跑在 8000。这里把接口代理过去，
// 这样前端代码里写死的都是相对路径 —— 生产构建产物由后端自己托管，
// 相对路径同样成立，两边不需要各写一套 base url。
const BACKEND = process.env.AGENT_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/sessions": { target: BACKEND, changeOrigin: true },
      "/healthz": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    // 构建产物由 FastAPI 的 StaticFiles 托管，不要 sourcemap，
    // 免得把整个 src 跟着发到线上。
    sourcemap: false,
  },
});
