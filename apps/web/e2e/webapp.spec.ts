import { expect as baseExpect, test, type Page } from "@playwright/test";
import { ANSWER, COMMAND, createAgent, setupFakeServer } from "./support.ts";

/*
 * 웹앱 스모크(설계 7.2, 수용 조건 A8). 빌더에서 발행하고 웹앱 공개를 켠 뒤 /apps/<slug>에서 대화한다.
 * preview.spec.ts와 같은 가짜 어댑터 서버(8502)를 쓴다.
 */

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

test("웹앱: 발행하고 /apps/<slug>에서 대화한다(승인 → 답, 새로 고침, 새 대화)", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const name = `e2e 웹앱 ${Date.now()}`;
  const agentId = await createAgent(page, name);
  const { slug } = await (await page.request.get(`/api/console/agents/${agentId}`)).json();
  await page.goto(`/agents/${agentId}`);
  await expect(page.getByRole("link", { name: "에이전트 로그" })).toHaveAttribute(
    "href",
    `/agents/${agentId}/logs`,
  );

  // 발행 전 API 탭은 발행부터 하라고 안내한다.
  await page.getByRole("tab", { name: "API" }).click();
  await expect(page.getByText("발행한 뒤에 웹앱으로 공개할 수 있습니다.")).toBeVisible();
  await page.getByRole("tab", { name: "발행" }).click();
  await page.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v1을(를) 발행했습니다.", { exact: true })).toBeVisible();

  // 웹앱 공개를 켜면 링크와 복사 버튼이 생긴다. 호출 예시는 발급한 키 자리(API_KEY)로 보인다.
  await page.getByRole("tab", { name: "API" }).click();
  await page.getByRole("switch", { name: "웹앱으로 공개" }).click();
  await expect(page.getByText("웹앱을 공개했습니다.", { exact: true })).toBeVisible();
  const path = `/apps/${slug}`;
  await expect(page.getByText(new URL(path, page.url()).href, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "링크 복사" })).toBeVisible();
  const examples = page.getByRole("list", { name: "호출 예시" });
  await expect(examples.locator("pre")).toHaveCount(4);
  await expect(examples.locator("pre").last()).toContainText("/api/v1/agent-tasks");
  await expect(examples.locator("pre").first()).toContainText("Bearer $API_KEY");

  await page.goto(path);
  const main = page.getByRole("main", { name });
  await expect(main.getByRole("heading", { name })).toBeVisible();
  await expect(main.getByText("v1", { exact: true })).toBeVisible();
  await expect(main.getByText("승인: 셸·쓰기 HTTP")).toBeVisible();
  await expect(page.getByText("초안 기준")).toHaveCount(0);

  const box = main.getByRole("textbox", { name: "메시지" });
  await box.fill("웹앱에서 보내는 첫 메시지");
  await box.press("Enter");

  const card = main.getByRole("region", { name: "실행 승인 요청" });
  await expect(card).toBeVisible();
  await expect(card.locator("pre")).toHaveText(COMMAND);
  await expect(page).toHaveURL(new RegExp(`${path}/s/[0-9a-f-]+$`));
  await card.getByRole("button", { name: "승인", exact: true }).click();
  await expect(main.getByText(ANSWER)).toBeVisible();
  await expect(main.getByRole("status", { name: "런 상태" })).toContainText("완료");
  const sessions = page.getByRole("navigation", { name: "대화 목록" });
  await expect(sessions.getByRole("link", { name: /웹앱에서 보내는 첫 메시지/ })).toBeVisible();

  // 웹앱 세션은 발행 버전을 쓰고 미리보기 세션과 섞이지 않는다.
  const sessionId = page.url().split("/").pop()!;
  const detail = await (await page.request.get(`/api/web/sessions/${sessionId}`)).json();
  expect(detail.source).toBe("webapp");
  expect(detail.active_run).toBeNull();

  // 새로 고치면 메시지 기록으로 다시 그린다.
  await page.reload();
  await expect(main.getByText(ANSWER)).toBeVisible();
  await expect(main.getByRole("button", { name: /shell/ })).toContainText("완료");

  // 새 대화는 빈 화면으로 시작하고, 목록에서 고르면 앞 대화로 돌아간다.
  await page.getByRole("button", { name: "새 대화" }).click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(main.getByText("메시지를 보내 대화를 시작하세요.")).toBeVisible();
  await sessions.getByRole("link", { name: /웹앱에서 보내는 첫 메시지/ }).click();
  await expect(page).toHaveURL(new RegExp(`${path}/s/${sessionId}$`));
  await expect(main.getByText(ANSWER)).toBeVisible();
});

test("API 키: 한 번만 보이는 키로 서비스 API를 부르고, 목록에는 앞부분만 남고, 폐기하면 401", async ({
  page,
  request,
}) => {
  const agentId = await createAgent(page, `e2e API 키 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "API" }).click();
  await page.getByRole("button", { name: "키 만들기" }).click();
  const form = page.getByRole("dialog", { name: "키 만들기" });
  const keyName = "e2e 정리 스크립트";
  await form.getByLabel("키 이름").fill(keyName);
  await form.getByRole("button", { name: "만들기", exact: true }).click();

  const created = page.getByRole("dialog", { name: "키를 만들었습니다" });
  await expect(created).toContainText(
    "이 값은 지금 한 번만 보여 줍니다. 안전한 곳에 복사해 두세요.",
  );
  await expect(created.locator("code")).toHaveText(/^sk-wf-\S+$/);
  const key = (await created.locator("code").textContent())!;
  // 서비스 API는 쿠키 없이 Bearer 키로만 부른다. request 픽스처는 브라우저 쿠키를 갖지 않는다.
  const bearer = { Authorization: `Bearer ${key}` };
  const info = await request.get("/api/v1/info", { headers: bearer });
  expect(info.status()).toBe(200);
  expect((await info.json()).agent_id).toBe(agentId);

  await created.getByRole("button", { name: "닫기" }).click();
  await expect(created).toBeHidden();
  const listed = await page.request.get(`/api/console/agents/${agentId}/api-keys`);
  const prefix = (await listed.json()).items[0].key_prefix as string;
  expect(key.startsWith(prefix) && key.length > prefix.length).toBeTruthy();
  const row = page.getByRole("row").filter({ hasText: keyName });
  await expect(row).toContainText(`${prefix}…`);
  await expect(page.getByText(key)).toHaveCount(0);

  await row.getByRole("button", { name: "폐기" }).click();
  const confirm = page.getByRole("alertdialog", { name: "API 키 폐기" });
  await confirm.getByRole("button", { name: "폐기" }).click();
  await expect(page.getByText("키를 폐기했습니다.", { exact: true })).toBeVisible();
  await expect(row).toContainText("폐기됨");
  expect((await request.get("/api/v1/info", { headers: bearer })).status()).toBe(401);
});

// 웹앱에서 메시지를 보내고 승인해 답까지 받은 뒤 그 대화의 세션 id를 돌려준다.
async function chat(page: Page, name: string, text: string): Promise<string> {
  const main = page.getByRole("main", { name });
  const box = main.getByRole("textbox", { name: "메시지" });
  await box.fill(text);
  await box.press("Enter");
  const card = main.getByRole("region", { name: "실행 승인 요청" });
  await card.getByRole("button", { name: "승인", exact: true }).click();
  await expect(main.getByText(ANSWER)).toBeVisible();
  await expect(main.getByRole("status", { name: "런 상태" })).toContainText("완료");
  await expect(page).toHaveURL(/\/s\/[0-9a-f-]+$/);
  return page.url().split("/").pop()!;
}

test("다시 발행하면 앞 대화는 '이전 발행 버전'으로 보이고 새 대화는 새 버전으로 시작한다", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const name = `e2e 발행 버전 ${Date.now()}`;
  const agentId = await createAgent(page, name);
  const agentPath = `/api/console/agents/${agentId}`;
  const { slug } = await (await page.request.get(agentPath)).json();
  // v1 발행과 웹앱 공개 화면은 앞 시험이 확인했으므로 API로 한다.
  expect((await page.request.post(`${agentPath}/publish`, { data: {} })).status()).toBe(201);
  const enabled = await page.request.patch(agentPath, { data: { webapp_enabled: true } });
  expect(enabled.ok()).toBeTruthy();

  const path = `/apps/${slug}`;
  const main = page.getByRole("main", { name });
  await page.goto(path);
  await expect(main.getByText("v1", { exact: true })).toBeVisible();
  const oldSession = await chat(page, name, "v1과 나눈 대화");

  // 빌더에서 다시 발행한다. 웹앱 세션은 만든 때의 발행 버전에 고정된다.
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "발행" }).click();
  await page.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v2을(를) 발행했습니다.", { exact: true })).toBeVisible();
  const v2 = (await (await page.request.get(agentPath)).json()).published_version_id;

  await page.goto(`${path}/s/${oldSession}`);
  await expect(main.getByText(ANSWER)).toBeVisible();
  await expect(main.getByText("이전 발행 버전", { exact: true })).toBeVisible();
  await expect(main.getByText("v2", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "새 대화" }).click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(main.getByText("v2", { exact: true })).toBeVisible();
  const newSession = await chat(page, name, "v2와 나눈 대화");
  const detail = await (await page.request.get(`/api/web/sessions/${newSession}`)).json();
  expect(detail.agent_version_id).toBe(v2);
  await expect(main.getByText("v2", { exact: true })).toBeVisible();
  await expect(main.getByText("이전 발행 버전", { exact: true })).toHaveCount(0);
});
