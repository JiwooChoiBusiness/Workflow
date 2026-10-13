import { expect, test } from "@playwright/test";

// 시험용 계정. 비어 있는 DATA_DIR이면 이 값으로 처음 설정을 한다.
const EMAIL = process.env.E2E_EMAIL ?? "e2e@local.test";
const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password-1234";

test.beforeEach(async ({ request }) => {
  const health = await request.get("/healthz").catch(() => null);
  test.skip(!health?.ok(), "백엔드가 떠 있지 않아 건너뛴다");
});

test("로그인하면 에이전트 목록으로 간다", async ({ page, request }) => {
  // 계정이 아직 없으면 API로 만든다. 설정 API는 로그인까지 해 주지 않는다.
  const setup = await request.get("/api/console/setup");
  if (setup.ok()) {
    const created = await request.post("/api/console/setup", {
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(created.status()).toBe(201);
  }

  await page.goto("/agents");
  await expect(page).toHaveURL(/\/login\?next=%2Fagents$/);
  await page.getByLabel("이메일").fill(EMAIL);
  await page.getByLabel("비밀번호").fill(PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();

  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByRole("heading", { level: 1, name: "에이전트" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "주 메뉴" })).toBeVisible();
});
