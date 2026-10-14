import { expect as baseExpect, test } from "@playwright/test";
import { approveAll, createAgent, setupFakeServer, waitingRun } from "./support.ts";

// 로그 화면(설계 7.2 /logs, /logs/sessions/:sid). preview.spec.ts와 같은 가짜 어댑터 서버(8502)를 쓴다.

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

test("로그: 세션 탭에서 미리보기 세션을 열어 런과 승인 결정을 보고, 런 탭을 상태로 거른다", async ({
  page,
}) => {
  const stamp = Date.now();
  const agentName = `e2e 로그 ${stamp}`;
  const title = `로그 화면 시험 ${stamp}`;
  const agentId = await createAgent(page, agentName);
  // 미리보기 런 하나를 API로 끝까지 돌린다(승인 1회). 세션 제목은 첫 메시지다.
  const { sessionId, runId } = await waitingRun(page.request, agentId, title);
  await approveAll(page.request, [runId]);
  await expect
    .poll(async () => (await (await page.request.get(`/api/console/runs/${runId}`)).json()).status)
    .toBe("succeeded");

  await page.goto("/logs");
  await expect(page.getByRole("tab", { name: "세션", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const sessionLink = page.getByRole("link", { name: title, exact: true });
  const sessionRow = page.getByRole("row").filter({ has: sessionLink });
  await expect(sessionRow).toContainText(agentName);
  await expect(sessionRow).toContainText("미리보기");
  await sessionLink.click();

  await expect(page).toHaveURL(new RegExp(`/logs/sessions/${sessionId}$`));
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  const runs = page.getByRole("list", { name: "이 세션의 런" });
  await expect(runs.getByRole("listitem")).toHaveCount(1);
  await expect(runs).toContainText("#1");
  await expect(runs).toContainText("완료");
  await page.getByRole("tab", { name: "승인·질문 1", exact: true }).click();
  const approvals = page.getByRole("tabpanel", { name: "승인·질문 1" });
  await expect(approvals).toContainText("shell");
  await expect(approvals.getByText("승인함")).toBeVisible();

  await page.getByRole("link", { name: "로그 목록" }).click();
  await page.getByRole("tab", { name: "런", exact: true }).click();
  await expect(page).toHaveURL(/[?&]tab=runs/);
  const runLink = page.getByRole("link", { name: title, exact: true });
  await expect(runLink).toBeVisible();
  const status = page.getByRole("combobox", { name: "상태" });
  await status.click();
  await page.getByRole("option", { name: "중지됨", exact: true }).click();
  await expect(page).toHaveURL(/[?&]status=cancelled/);
  await expect(runLink).toHaveCount(0);
  await status.click();
  await page.getByRole("option", { name: "완료", exact: true }).click();
  await expect(page).toHaveURL(/[?&]status=succeeded/);
  await expect(runLink).toBeVisible();
});
