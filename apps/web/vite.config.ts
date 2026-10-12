import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 개발 서버의 /api 요청은 compose로 띄운 gateway로 넘긴다.
const gatewayPort = process.env.GATEWAY_HTTP_PORT ?? "8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": `http://localhost:${gatewayPort}` },
  },
  test: {
    environment: "node",
  },
});
