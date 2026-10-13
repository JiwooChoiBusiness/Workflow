import { defineConfig } from "@playwright/test";

// 스모크는 8502에 띄운 시험용 서버(따로 둔 DATA_DIR)를 대상으로 한다. 평소 쓰는 8500의 데이터는 건드리지 않는다.
// 서버가 없으면 각 테스트가 스스로 건너뛴다(e2e/*.spec.ts).
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8502";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  workers: 1,
  reporter: "list",
  use: {
    baseURL,
    // 브라우저를 따로 내려받지 않고 Windows에 깔린 Edge를 쓴다. 다른 환경은 E2E_BROWSER_CHANNEL로 바꾼다.
    channel: process.env.E2E_BROWSER_CHANNEL ?? "msedge",
    headless: true,
    locale: "ko-KR",
    trace: "retain-on-failure",
  },
});
