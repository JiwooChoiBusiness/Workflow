import { expect as baseExpect, test } from "@playwright/test";
import {
  ANSWER,
  approvalCard,
  createAgent,
  preview,
  previewSessionId,
  runStatus,
  send,
  setupFakeServer,
  waitRunnerIdle,
} from "./support.ts";

// 빌더 미리보기의 작업공간 패널(설계 3.14, 7.3). preview.spec.ts와 같은 가짜 어댑터 서버(8502)를 쓴다.

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

test("작업공간 패널: 상태와 디스크를 보고 초기화한 뒤 같은 세션에서 이어서 대화한다", async ({
  page,
}) => {
  const agentId = await createAgent(page, `e2e 작업공간 ${Date.now()}`);
  await page.goto(`/agents/${agentId}`);
  const panel = preview(page);
  const open = panel.getByRole("button", { name: "작업공간 보기" });
  // 세션은 첫 메시지 때 생기므로 그 전에는 열 수 없다.
  await expect(open).toBeDisabled();

  await send(page, "작업공간을 만드는 첫 메시지");
  await approvalCard(page).getByRole("button", { name: "승인", exact: true }).click();
  await expect(panel.getByText(ANSWER)).toBeVisible();
  await expect(runStatus(page)).toContainText("완료");
  const sessionId = await previewSessionId(page, agentId);
  expect(sessionId).not.toBeNull();
  // 패널은 열 때 한 번 읽는다. 런 마무리가 끝나 작업공간이 "대기"로 돌아온 뒤에 연다.
  await waitRunnerIdle(page.request);

  await open.click();
  const dialog = page.getByRole("dialog", { name: "작업공간" });
  await expect(dialog.getByText("대기", { exact: true })).toBeVisible();
  await expect(dialog).toContainText(/\S+ \/ 한도 \S+/);
  await dialog.getByRole("button", { name: "작업공간 초기화" }).click();
  const confirm = page.getByRole("alertdialog", { name: "작업공간을 초기화할까요?" });
  await expect(confirm).toContainText("대화 기록과 보관된 결과 파일은 남습니다.");
  await confirm.getByRole("button", { name: "작업공간 초기화" }).click();
  await expect(page.getByText("작업공간을 초기화했습니다.", { exact: true })).toBeVisible();
  await expect(confirm).toBeHidden();
  await dialog.getByRole("button", { name: "닫기" }).click();
  await expect(dialog).toBeHidden();

  await send(page, "초기화한 뒤 보내는 메시지");
  await approvalCard(page).getByRole("button", { name: "승인", exact: true }).click();
  await expect(panel.getByText(ANSWER)).toHaveCount(2);
  await expect(runStatus(page)).toContainText("완료");
  expect(await previewSessionId(page, agentId)).toBe(sessionId);
});
