import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { request, type FullConfig } from "@playwright/test";
import { AUTH_STATE } from "./auth-state.ts";
import { apiLogin, ensureAccount, serverUp } from "./support.ts";

/*
 * 스펙 파일들이 같이 쓸 로그인 세션을 한 번만 만든다. 서버의 로그인 한도가 분당 5회라 파일마다 로그인하면
 * 한도가 풀릴 때까지 기다려야 했다. 화면 로그인은 login.spec.ts만 한다. 서버가 없거나 로그인하지 못하면
 * 빈 세션을 남기고 각 시험이 스스로 건너뛴다(support.ts setupFakeServer).
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const context = await request.newContext({ baseURL: config.projects[0]?.use.baseURL });
  try {
    if (await serverUp(context)) {
      await ensureAccount(context);
      await apiLogin(context);
    }
    await mkdir(dirname(AUTH_STATE), { recursive: true });
    await context.storageState({ path: AUTH_STATE });
  } finally {
    await context.dispose();
  }
}
