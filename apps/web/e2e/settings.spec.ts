import { expect as baseExpect, test, type APIRequestContext } from "@playwright/test";
import { setupFakeServer } from "./support.ts";

// 설정 화면(설계 7.2 /settings/*). preview.spec.ts와 같은 가짜 어댑터 서버(8502)를 쓴다.

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

async function retentionDays(request: APIRequestContext): Promise<number> {
  const workspace = await (await request.get("/api/console/workspace")).json();
  return workspace.settings.run_event_retention_days as number;
}

test("설정: LLM의 가짜 어댑터 안내, HTTP 허용 목록, 워크스페이스 보관 기간 저장과 원복", async ({
  page,
}) => {
  const menu = page.getByRole("navigation", { name: "설정 메뉴" });

  await page.goto("/settings/llm");
  await expect(page.getByRole("heading", { level: 1, name: "Gemini 연결" })).toBeVisible();
  await expect(
    page.getByText("가짜 어댑터(LLM_FAKE=true)로 동작 중이라 실제 Gemini를 부르지 않습니다.", {
      exact: true,
    }),
  ).toBeVisible();

  await menu.getByRole("link", { name: "HTTP 허용 목록", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/http-allowlist$/);
  // 서버가 처음 뜰 때 만든 기본 목록이다. 적는 법 안내의 pypi.org 예시와 섞이지 않게 읽은 규칙(dd)에서 찾는다.
  await expect(page.getByRole("definition").getByText("pypi.org", { exact: true })).toBeVisible();

  await menu.getByRole("link", { name: "워크스페이스", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/workspace$/);
  const before = await retentionDays(page.request);
  const changed = before < 3650 ? before + 1 : before - 1;
  const field = page.getByLabel("런 이벤트 보관 기간(일)");
  const save = page.getByRole("button", { name: "저장", exact: true });
  await expect(field).toHaveValue(String(before));
  await field.fill(String(changed));
  await save.click();
  await expect(page.getByText("워크스페이스 설정을 저장했습니다.", { exact: true })).toBeVisible();
  expect(await retentionDays(page.request)).toBe(changed);

  // 원래 값으로 되돌린다. 앞 토스트가 아직 떠 있을 수 있어 서버 값으로 확인한다.
  await expect(field).toHaveValue(String(changed));
  await expect(save).toBeDisabled();
  await field.fill(String(before));
  await save.click();
  await expect.poll(() => retentionDays(page.request)).toBe(before);
  await expect(field).toHaveValue(String(before));
});
