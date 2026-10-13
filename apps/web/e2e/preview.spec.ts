import { readFile } from "node:fs/promises";
import { expect as baseExpect, test, type Cookie } from "@playwright/test";
import {
  approvalCard,
  approveAll,
  createAgent,
  ensureAccount,
  fakeLlm,
  preview,
  previewSessionId,
  runStatus,
  send,
  serverUp,
  uiLogin,
  waitingRuns,
  waitRunnerIdle,
} from "./support.ts";

/*
 * 빌더 미리보기 스모크(설계 10장 M7). 시험용 DATA_DIR로 가짜 어댑터 서버를 8502에 띄우고 돌린다. 서버는
 * WEB_DIST_DIR(기본 apps/web/dist)의 빌드를 서빙하므로 먼저 빌드한다.
 *   SERVER_PORT=8502 DATA_DIR=C:\wf-scratch LLM_FAKE=true uv run platform serve
 *   corepack pnpm --dir apps/web e2e
 */

// 런은 서버 워커(동시 1)를 거쳐 돌아 화면 반영이 몇 초 늦을 수 있다.
const expect = baseExpect.configure({ timeout: 15_000 });

// 가짜 어댑터 기본 각본: shell("echo fake-llm") 한 번 → 승인 → "도구 실행 결과를 확인했습니다."
const COMMAND = "echo fake-llm";
const ANSWER = "도구 실행 결과를 확인했습니다.";

// 로그인은 한 번만 하고 쿠키를 시험마다 넣는다. 서버의 로그인 레이트 리밋이 분당 5회다.
let cookies: Cookie[] | null = null;
let fake = false;

test.beforeAll(async ({ playwright, baseURL }) => {
  // 로그인 레이트 리밋에 걸리면 1분 가까이 기다린다.
  test.setTimeout(90_000);
  const request = await playwright.request.newContext({ baseURL });
  try {
    if (!(await serverUp(request))) return;
    await ensureAccount(request);
    fake = await fakeLlm(request);
    cookies = (await request.storageState()).cookies;
  } finally {
    await request.dispose();
  }
});

test.beforeEach(async ({ context }) => {
  test.skip(!cookies, "백엔드가 떠 있지 않거나 로그인하지 못해 건너뛴다");
  test.skip(!fake, "LLM_FAKE=true로 띄운 서버에서만 돈다");
  await context.addCookies(cookies!);
});

test.afterEach(async ({ request }) => {
  if (await serverUp(request)) await waitRunnerIdle(request);
});

test("빌더 미리보기: 에이전트 생성·모델 선택·저장 뒤 승인 카드, 도구 카드, 답, 파일 내려받기", async ({
  page,
  context,
}) => {
  test.setTimeout(120_000);
  // 이 시험은 화면으로 로그인한다.
  await context.clearCookies();
  await uiLogin(page);
  await expect(page).toHaveURL(/\/agents$/);

  await page.getByRole("button", { name: "새 에이전트" }).click();
  const dialog = page.getByRole("dialog", { name: "새 에이전트" });
  await dialog.getByLabel("이름").fill(`e2e 미리보기 ${Date.now()}`);
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(page).toHaveURL(/\/agents\/[0-9a-f-]+$/);
  const agentId = page.url().split("/").pop()!;

  await page.getByRole("combobox", { name: "모델" }).click();
  await page.getByRole("option", { name: /gemini-3\.8-flash/ }).click();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();
  const draft = await page.request.get(`/api/console/agents/${agentId}/draft`);
  expect((await draft.json()).model.model_id).toBe("gemini-3.8-flash");

  const panel = preview(page);
  await expect(panel.getByText("초안 기준")).toBeVisible();
  await expect(panel.getByText("승인: 셸·쓰기 HTTP")).toBeVisible();

  // 첨부 파일을 붙여 보낸다. 올린 파일은 내 메시지에 내려받기 링크로 남는다.
  const csv = "이름,값\na,1\n";
  await panel
    .getByLabel("첨부할 파일 고르기")
    .setInputFiles({ name: "입력.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await expect(panel.getByRole("list", { name: "첨부할 파일" })).toContainText("입력.csv");
  await send(page, "작업공간 파일 목록을 보여줘");

  // 승인 카드: 도구 이름과 명령 전문, 작업 디렉터리
  const card = approvalCard(page);
  await expect(card).toBeVisible();
  await expect(card.locator("pre")).toHaveText(COMMAND);
  await expect(card).toContainText("shell");
  await expect(card).toContainText("작업 디렉터리");
  await expect(runStatus(page)).toContainText("입력 기다리는 중");
  await card.getByRole("button", { name: "승인", exact: true }).click();

  // 도구 카드(출력·종료 코드)와 어시스턴트 답
  await expect(panel.getByText(ANSWER)).toBeVisible();
  await expect(card).toBeHidden();
  await expect(runStatus(page)).toContainText("완료");
  await expect(panel.getByText("승인함")).toBeVisible();
  const tool = panel.getByRole("button", { name: /shell/ });
  await expect(tool).toContainText(COMMAND);
  await expect(tool).toContainText("완료");
  await tool.click();
  await expect(panel.getByText("fake-llm", { exact: true })).toBeVisible();
  await expect(panel.getByText("종료 코드 0")).toBeVisible();

  // 내 메시지의 첨부 파일 내려받기
  const attachment = panel.getByRole("link", { name: "입력.csv 내려받기" });
  const [download] = await Promise.all([page.waitForEvent("download"), attachment.click()]);
  expect(await readFile((await download.path())!, "utf8")).toBe(csv);

  // 런이 만든 파일이 있으면 내려받는다. 가짜 어댑터 기본 각본은 파일을 만들지 않는다.
  const changed = panel.getByRole("heading", { name: "변경된 파일" });
  if (await changed.isVisible()) {
    const link = panel.getByRole("link", { name: /내려받기$/ }).last();
    const [output] = await Promise.all([page.waitForEvent("download"), link.click()]);
    expect(await output.path()).toBeTruthy();
  }

  // 세션 초기화: 새 대화로 시작하고 다음 메시지에서 새 세션을 만든다.
  expect(await previewSessionId(page, agentId)).not.toBeNull();
  await panel.getByRole("button", { name: "세션 초기화" }).click();
  await expect(panel.getByText("메시지를 보내 대화를 시작하세요.")).toBeVisible();
  expect(await previewSessionId(page, agentId)).toBeNull();
});

test("승인을 기다리는 중에 새로 고치면 승인 카드가 돌아오고 이어서 승인할 수 있다", async ({
  page,
}) => {
  const agentId = await createAgent(page, `e2e 승인 복원 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  await send(page, "새로 고침 시험");
  await expect(approvalCard(page)).toBeVisible();

  const resubscribed = page.waitForRequest(/\/events\?after=0&run_id=/);
  await page.reload();
  await resubscribed;
  const card = approvalCard(page);
  await expect(card).toBeVisible();
  await expect(card.locator("pre")).toHaveText(COMMAND);
  await expect(preview(page).getByText("새로 고침 시험")).toBeVisible();
  await expect(runStatus(page)).toContainText("입력 기다리는 중");

  await card.getByRole("button", { name: "승인", exact: true }).click();
  await expect(preview(page).getByText(ANSWER)).toBeVisible();
  await expect(runStatus(page)).toContainText("완료");

  // 끝난 대화도 새로 고치면 메시지 기록으로 다시 그린다.
  await page.reload();
  await expect(preview(page).getByText(ANSWER)).toBeVisible();
  await expect(preview(page).getByRole("button", { name: /shell/ })).toContainText("완료");
});

test("진행 중인 런은 새로 고침 뒤 다시 구독해 끝까지 보여 준다", async ({ page }) => {
  test.setTimeout(90_000);
  const agentId = await createAgent(page, `e2e 재구독 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  await send(page, "대기열에서 기다리는 런");
  await expect(approvalCard(page)).toBeVisible();

  // 다른 세션의 런 여섯 개를 먼저 승인해 워커를 붙잡아 두면 이 런은 대기열에서 기다린다.
  await approveAll(page.request, await waitingRuns(page.request, agentId, 6));
  await approvalCard(page).getByRole("button", { name: "승인", exact: true }).click();
  await expect(runStatus(page)).toContainText(/대기열|실행 중/);

  const resubscribed = page.waitForRequest(/\/events\?after=0&run_id=/);
  await page.reload();
  await resubscribed;
  await expect(runStatus(page)).toContainText(/대기열|실행 중/);
  await expect(preview(page).getByRole("button", { name: "중지" })).toBeVisible();
  await expect(preview(page).getByText(ANSWER)).toBeVisible({ timeout: 30_000 });
  await expect(runStatus(page)).toContainText("완료");
  await expect(preview(page).getByText("승인함")).toBeVisible();
});

test("중지 버튼: 승인 대기 중인 런과 대기열의 런을 멈춘다", async ({ page }) => {
  test.setTimeout(90_000);
  const agentId = await createAgent(page, `e2e 중지 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  const panel = preview(page);

  await send(page, "승인 대기 중에 멈추기");
  await expect(approvalCard(page)).toBeVisible();
  await panel.getByRole("button", { name: "중지" }).click();
  await expect(runStatus(page)).toContainText("중지됨");
  await expect(approvalCard(page)).toBeHidden();
  await expect(panel.getByText("런을 중지했습니다")).toBeVisible();
  await expect(panel.getByRole("button", { name: /shell/ })).toContainText("취소됨");

  // 같은 세션에 다시 보내 대기열에 넣고 멈춘다.
  await panel.getByRole("button", { name: "세션 초기화" }).click();
  await send(page, "대기열에서 멈추기");
  await expect(approvalCard(page)).toBeVisible();
  await approveAll(page.request, await waitingRuns(page.request, agentId, 6));
  await approvalCard(page).getByRole("button", { name: "승인", exact: true }).click();
  await expect(runStatus(page)).toContainText(/대기열|실행 중/);
  await panel.getByRole("button", { name: "중지" }).click();
  await expect(runStatus(page)).toContainText("중지됨");
  await expect(panel.getByRole("button", { name: "보내기" })).toBeVisible();
  const sessionId = await previewSessionId(page, agentId);
  const detail = await (await page.request.get(`/api/console/sessions/${sessionId}`)).json();
  expect(detail.active_run).toBeNull();
  expect(detail.last_run.status).toBe("cancelled");
});

test("'이 세션에서는 묻지 않기'로 승인하면 칩이 켜지고, 끄면 다시 승인을 묻는다", async ({
  page,
}) => {
  const agentId = await createAgent(page, `e2e 자동 승인 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  const panel = preview(page);

  await send(page, "첫 요청");
  const card = approvalCard(page);
  await expect(card).toBeVisible();
  await card.getByRole("checkbox", { name: "이 세션에서는 묻지 않기" }).check();
  await card.getByRole("button", { name: "승인", exact: true }).click();
  await expect(panel.getByText(ANSWER)).toHaveCount(1);
  await expect(panel.getByText("자동 승인 켜짐")).toBeVisible();
  await expect(panel.getByText("승인함 · 이 세션에서는 더 묻지 않음")).toBeVisible();

  // 자동 승인 중에는 카드 없이 실행된다.
  await send(page, "두 번째 요청");
  await expect(panel.getByText(ANSWER)).toHaveCount(2);
  await expect(card).toBeHidden();

  // 새로 고쳐도 칩이 남는다(세션의 approval_override).
  await page.reload();
  await expect(panel.getByText("자동 승인 켜짐")).toBeVisible();

  await panel.getByRole("button", { name: "끄기" }).click();
  await expect(panel.getByText("자동 승인 켜짐")).toBeHidden();
  const sessionId = await previewSessionId(page, agentId);
  const detail = await (await page.request.get(`/api/console/sessions/${sessionId}`)).json();
  expect(detail.approval_override).toBeNull();

  await send(page, "세 번째 요청");
  await expect(card).toBeVisible();
  await expect(card.locator("pre")).toHaveText(COMMAND);
});
