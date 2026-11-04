import { fileURLToPath } from "node:url";

// 전역 준비(global-setup.ts)가 한 번 로그인해 남기는 세션 쿠키. 출력 폴더(test-results)라 실행할 때마다
// 비워지고 저장소에도 들어가지 않는다.
export const AUTH_STATE = fileURLToPath(
  new URL("../test-results/.auth/console.json", import.meta.url),
);
