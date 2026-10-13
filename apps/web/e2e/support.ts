import { expect, type APIRequestContext, type Page } from "@playwright/test";

// 시험용 계정. 비어 있는 DATA_DIR이면 이 값으로 처음 설정을 한다.
export const EMAIL = process.env.E2E_EMAIL ?? "e2e@local.test";
export const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password-1234";

export async function serverUp(request: APIRequestContext): Promise<boolean> {
  const health = await request.get("/healthz").catch(() => null);
  return Boolean(health?.ok());
}

// 로그인 레이트 리밋(분당 5회)에 걸리면 Retry-After만큼 기다렸다가 한 번 더 한다. 거절된 시도는 세지 않는다.
export async function apiLogin(request: APIRequestContext): Promise<boolean> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await request.post("/api/console/auth/login", {
      data: { email: EMAIL, password: PASSWORD },
    });
    if (response.status() !== 429) return response.ok();
    const seconds = Number(response.headers()["retry-after"] ?? "60");
    await new Promise((resolve) => setTimeout(resolve, Math.min(seconds, 65) * 1000));
  }
  return false;
}

export async function uiLogin(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("이메일").fill(EMAIL);
  await page.getByLabel("비밀번호").fill(PASSWORD);
  const limited = page.getByText(/초 뒤에 다시 시도하세요/);
  const agents = page.getByRole("heading", { level: 1, name: "에이전트" });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await page.getByRole("button", { name: "로그인" }).click();
    await expect(limited.or(agents)).toBeVisible({ timeout: 15_000 });
    if (await agents.isVisible()) return;
    const seconds = Number((await limited.textContent())?.match(/(\d+)초/)?.[1] ?? "60");
    await page.waitForTimeout(Math.min(seconds, 65) * 1000);
  }
  await expect(agents).toBeVisible();
}

// 가짜 어댑터(LLM_FAKE=true)로 띄운 서버인지. 실제 Gemini로 띄운 서버에서는 미리보기 시험을 건너뛴다.
export async function fakeLlm(request: APIRequestContext): Promise<boolean> {
  if (!(await apiLogin(request))) return false;
  const models = await request.get("/api/console/models");
  return models.ok() && Boolean((await models.json()).fake);
}

// 앞 시험이 대기열에 남긴 런이 다음 시험을 늦추지 않게 워커가 빌 때까지 기다린다.
export async function waitRunnerIdle(request: APIRequestContext): Promise<void> {
  await expect
    .poll(
      async () => {
        const health = await (await request.get("/healthz")).json();
        return health.runner.active + health.runner.queued;
      },
      { timeout: 30_000 },
    )
    .toBe(0);
}

// 계정이 아직 없으면 API로 만든다. 설정 API는 로그인까지 해 주지 않는다.
export async function ensureAccount(request: APIRequestContext): Promise<void> {
  const setup = await request.get("/api/console/setup");
  if (setup.ok() && (await setup.json()).needs_setup) {
    const created = await request.post("/api/console/setup", {
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(created.status()).toBe(201);
  }
}

// 새 에이전트의 초안은 도구 네 묶음(shell 포함)과 승인 모드 all_shell이다. 가짜 어댑터는 shell을 한 번 부른다.
export async function createAgent(page: Page, name: string): Promise<string> {
  const response = await page.request.post("/api/console/agents", { data: { name } });
  expect(response.status()).toBe(201);
  return (await response.json()).id as string;
}

export function preview(page: Page) {
  return page.getByRole("complementary", { name: "미리보기" });
}

export function approvalCard(page: Page) {
  return preview(page).getByRole("region", { name: "실행 승인 요청" });
}

export function runStatus(page: Page) {
  return preview(page).getByRole("status", { name: "런 상태" });
}

export async function send(page: Page, text: string): Promise<void> {
  const box = preview(page).getByRole("textbox", { name: "메시지" });
  await box.fill(text);
  await box.press("Enter");
}

export async function previewSessionId(page: Page, agentId: string): Promise<string | null> {
  return page.evaluate((key) => window.localStorage.getItem(key), `wf.preview-session.${agentId}`);
}

/*
 * 같은 에이전트의 다른 세션에 승인을 기다리는 런을 count개 만든다. 이 런들을 승인하면 워커(동시 1)가
 * 차례로 처리하는 동안 뒤에 승인한 런은 대기열에 머문다. 가짜 어댑터의 런은 1초도 걸리지 않아서
 * "진행 중"인 런을 붙잡아 두려면 이렇게 대기열을 채운다.
 */
export async function waitingRuns(
  request: APIRequestContext,
  agentId: string,
  count: number,
): Promise<string[]> {
  const runIds: string[] = [];
  for (let i = 0; i < count; i += 1) {
    const created = await request.post(`/api/console/agents/${agentId}/preview/sessions`, {
      data: {},
    });
    expect(created.status()).toBe(201);
    const sessionId = (await created.json()).id as string;
    // SSE 응답은 run.completed(waiting_input)에서 닫힌다.
    const response = await request.post(`/api/console/sessions/${sessionId}/messages`, {
      data: { text: `대기열 채우기 ${i + 1}` },
    });
    expect(response.ok()).toBeTruthy();
    expect(await response.text()).toContain('"status":"waiting_input"');
    runIds.push(response.headers()["x-run-id"]);
  }
  return runIds;
}

export async function approveAll(request: APIRequestContext, runIds: string[]): Promise<void> {
  for (const runId of runIds) {
    const response = await request.post(`/api/console/runs/${runId}/input`, {
      data: { decision: "approve" },
    });
    expect(response.status()).toBe(202);
  }
}
