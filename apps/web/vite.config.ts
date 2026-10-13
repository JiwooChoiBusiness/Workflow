import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));

export default defineConfig(({ mode }) => {
  // 환경변수 SERVER_PORT가 있으면 그것을, 없으면 저장소 루트 .env의 값을 쓴다. 다른 키는 가져오지 않는다.
  const serverPort = loadEnv(mode, repoRoot, "SERVER_PORT").SERVER_PORT || "8500";
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      port: 5173,
      // localhost로 적으면 Node가 ::1을 먼저 시도해 127.0.0.1에만 바인드한 서버에 닿지 못한다.
      proxy: { "/api": `http://127.0.0.1:${serverPort}` },
    },
    test: {
      environment: "node",
      include: ["src/**/*.test.{ts,tsx}"],
      setupFiles: ["src/test/setup.ts"],
    },
  };
});
