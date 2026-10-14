import { expect as baseExpect, test } from "@playwright/test";
import {
  createAgent,
  preview,
  previewSessionId,
  runStatus,
  send,
  setupFakeServer,
} from "./support.ts";

/*
 * 오케스트레이션 스모크(설계 O2). 지휘자가 하위 에이전트 shellbot을 부르고, shellbot의 셸 승인을 미리보기의
 * 하위 에이전트 카드 안에서 한 뒤 둘 다 끝난다. 가짜 어댑터 각본은 서버 전체가 순서대로 한 번만 쓰므로 이
 * 시험만 각본으로 새로 띄운 서버에서 돈다. 다른 시험은 기본 각본 서버에서 돌고, 이 파일은 건너뛴다.
 *   SERVER_PORT=8502 DATA_DIR=C:\wf-scratch LLM_FAKE=true \
 *     LLM_FAKE_SCRIPT=apps/api/tests/fixtures/fake_llm/orchestration_child_shell.json uv run platform serve
 *   E2E_FAKE_SCRIPT=orchestration_child_shell corepack pnpm --dir apps/web e2e orchestration
 */

const expect = baseExpect.configure({ timeout: 15_000 });

test.skip(
  process.env.E2E_FAKE_SCRIPT !== "orchestration_child_shell",
  "LLM_FAKE_SCRIPT=orchestration_child_shell.json으로 띄운 서버에서만 돈다",
);

setupFakeServer();

test("지휘자: 빌더에서 하위 에이전트를 고르고, 카드 안에서 자식을 승인하고, 로그에서 하위 런을 본다", async ({
  page,
}) => {
  test.setTimeout(120_000);
  // 각본의 call_agent는 agent="shellbot"이다. 앞 실행이 남긴 것은 지우고 발행하지 않은 새 에이전트로 만든다.
  const listed = (await (await page.request.get("/api/console/agents")).json()) as {
    items: { id: string; slug: string }[];
  };
  for (const old of listed.items.filter((agent) => agent.slug === "shellbot")) {
    // 본문 없는 변경 요청은 CSRF 검사가 이 헤더를 요구한다.
    const removed = await page.request.delete(`/api/console/agents/${old.id}`, {
      headers: { "X-Requested-With": "fetch" },
    });
    expect(removed.status()).toBe(204);
  }
  const created = await page.request.post("/api/console/agents", {
    data: { name: "셸 담당", slug: "shellbot", description: "셸 명령을 실행하고 결과를 알려 준다" },
  });
  expect(created.status()).toBe(201);
  const childId = (await created.json()).id as string;
  const conductorId = await createAgent(page, `e2e 지휘자 ${Date.now()}`);

  // 빌더: 도구와 스킬 탭의 하위 에이전트 묶음에서 고르고 저장한다. 발행 전이라 경고가 붙는다.
  await page.goto(`/agents/${conductorId}`);
  await page.getByRole("tab", { name: "도구와 스킬" }).click();
  await page.getByRole("checkbox", { name: "셸 담당을(를) 하위 에이전트로 쓰기" }).check();
  await expect(page.getByText("미발행", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();
  await expect(page.getByText(/^하위 에이전트 경고 1건/)).toBeVisible();
  const draft = await (await page.request.get(`/api/console/agents/${conductorId}/draft`)).json();
  expect(draft.sub_agents).toEqual([
    {
      agent_id: childId,
      result: { format: "text", schema: null, files: true },
      approval_mode: "inherit",
    },
  ]);

  // 미리보기: 하위 에이전트 카드가 생기고 그 안에 shellbot의 승인 요청이 뜬다.
  await send(page, "셸 담당에게 맡겨 줘");
  const panel = preview(page);
  const card = panel.getByRole("region", { name: "하위 에이전트 셸 담당" });
  await expect(card).toBeVisible();
  await expect(card.getByText("초안", { exact: true })).toBeVisible();
  const approval = card.getByRole("region", { name: "shellbot(셸 담당)의 승인 요청" });
  await expect(approval).toBeVisible();
  await expect(approval.locator("pre")).toHaveText("echo child-ok");
  await expect(runStatus(page)).toContainText("하위 에이전트 셸 담당 실행 중");

  // 새로 고쳐도 카드와 승인 요청이 돌아오고(active_run.child, 자식 세션 재구독) 그대로 승인할 수 있다.
  await page.reload();
  await expect(approval).toBeVisible();
  await expect(runStatus(page)).toContainText("하위 에이전트 셸 담당 실행 중");
  await approval.getByRole("button", { name: "승인", exact: true }).click();

  // 자식이 끝나면 부모가 결과를 받아 답한다.
  await expect(panel.getByText("하위 에이전트가 셸 결과 child-ok를 돌려줬다.")).toBeVisible();
  await expect(runStatus(page)).toContainText("완료");
  const header = card.getByRole("button", { name: /셸 담당/ });
  await expect(header).toContainText("완료");

  // 펼치면 자식의 도구 카드와 답이 그대로 보인다.
  await header.click();
  await expect(card.getByText("셸 결과: child-ok", { exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: /^shell/ })).toContainText("완료");

  // 로그: 부모 런 상세의 하위 런 탭과 하위 포함 합계, 자식 런 상세의 부모 링크
  const sessionId = await previewSessionId(page, conductorId);
  await page.goto(`/logs/sessions/${sessionId}`);
  await page.getByRole("tab", { name: "하위 런 1", exact: true }).click();
  const children = page.getByRole("list", { name: "하위 런" });
  await expect(children).toContainText("셸 담당");
  await expect(page.getByText("하위 포함 합계")).toBeVisible();
  await children.getByRole("link", { name: "세션 열기" }).click();
  await expect(page.getByText("부모 런")).toBeVisible();
});
