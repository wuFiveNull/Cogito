import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物输出到 .workspace/web/dist，由 FastAPI 托管。
// dev 模式下通过 proxy 把 /api 转发到 FastAPI (默认 8081)。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../.workspace/web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8081",
        changeOrigin: true,
        // 同时转发 WebSocket（/api/chat/ws），否则 dev 模式下聊天连不上后端
        ws: true,
      },
    },
  },
});
