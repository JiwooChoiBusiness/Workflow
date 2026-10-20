import { expect as baseExpect, test, type Locator, type Page } from "@playwright/test";
import { ANSWER, COMMAND, createAgent, setupFakeServer } from "./support.ts";

/*
 * 챗플로우(2단계 설계 7.3 ChatflowPreview, 7.4 웹앱 채팅, M17). 기본 대화 템플릿으로 만들어 편집기 미리보기에서
 * 두 턴 대화하고(두 번째 턴의 llm 입력에 첫 턴이 메모리로 들어간다), 발행해 웹앱 /flows/:slug에서 대화한 뒤
 * 새로 고쳐도 대화가 남는다. agent 노드의 셸 승인은 대화 안의 승인 카드로 한다. 가짜 어댑터 서버(8502)를 쓴다.
 */

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

async function say(scope: Locator | Page, text: string): Promise<void> {
  const box = scope.getByRole("textbox", { name: "메시지" });
  await box.fill(text);
  await box.press("Enter");
}

function node(id: string, type: string, x: number, config: Record<string, unknown>, title = id) {
  return { id, type, title, position: { x, y: 0 }, config };
}

test("챗플로우: 기본 대화로 만들어 미리보기 두 턴, 발행 뒤 웹앱에서 대화하고 새로 고쳐도 남는다", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const name = `e2e 챗플로우 ${Date.now()}`;
  await page.goto("/workflows");
  await page.getByRole("button", { name: "새 워크플로우" }).first().click();
  const dialog = page.getByRole("dialog", { name: "새 워크플로우" });
  await dialog.getByLabel("이름").fill(name);
  await dialog.getByRole("radio", { name: /^챗플로우/ }).click();
  await dialog.getByRole("radio", { name: /^기본 대화/ }).click();
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]{36}$/);
  const workflowId = new URL(page.url()).pathname.split("/").pop()!;

  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  // llm → answer 사이에 분기가 없어 answer 카드에 미리 흘리기 표시가 붙는다.
  await expect(canvas.getByTestId("flow-node-answer_1").getByText("스트리밍 가능")).toBeVisible();

  // 상단 미리보기 단추가 미리보기 탭의 대화를 연다.
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await expect(panel.getByRole("tab", { name: "미리보기" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await say(panel, "첫 질문");
  await expect(panel.getByText("가짜 응답: 첫 질문")).toBeVisible();
  const status = panel.getByRole("status", { name: "워크플로우 런 상태" });
  await expect(status).toContainText("완료");
  for (const id of ["start", "llm_1", "answer_1"]) {
    await expect(
      canvas.getByTestId(`flow-node-${id}`).locator("[data-run-status]"),
    ).toHaveAttribute("data-run-status", "succeeded");
  }

  await say(panel, "둘째 질문");
  await expect(panel.getByText("가짜 응답: 둘째 질문")).toBeVisible();
  await expect(status).toContainText("완료");
  // 첫 메시지가 대화 제목이 된다.
  await expect(panel.getByRole("combobox", { name: "미리보기 대화" })).toContainText("첫 질문");

  // 실행 과정 탭은 지금 턴의 노드 타임라인이다.
  await panel.getByRole("tab", { name: "실행 과정" }).click();
  await expect(panel.getByTestId("node-run-llm_1")).toHaveAttribute("data-status", "succeeded");
  await panel.getByRole("tab", { name: "대화", exact: true }).click();

  // 두 번째 턴의 llm 입력에 첫 턴이 메모리로 들어갔다.
  const runs = await (
    await page.request.get(`/api/console/workflows/${workflowId}/runs?limit=1`)
  ).json();
  const runId = runs.items[0].id as string;
  const nodeRuns = await (await page.request.get(`/api/console/wf-runs/${runId}/node-runs`)).json();
  const llm = (nodeRuns.items as { id: string; node_id: string }[]).find(
    (item) => item.node_id === "llm_1",
  )!;
  const detail = await (
    await page.request.get(`/api/console/wf-runs/${runId}/node-runs/${llm.id}`)
  ).json();
  expect(detail.inputs.memory).toEqual([
    { role: "user", text: "첫 질문" },
    { role: "assistant", text: "가짜 응답: 첫 질문" },
  ]);

  // 새로 고쳐도 같은 미리보기 대화가 열린다.
  await page.reload();
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await expect(panel.getByText("가짜 응답: 둘째 질문")).toBeVisible();

  // 발행하고 API 탭에서 웹앱 공개와 키를 다룬다.
  await panel.getByRole("tab", { name: "발행" }).click();
  await panel.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v1을(를) 발행했습니다.", { exact: true })).toBeVisible();
  await panel.getByRole("tab", { name: "API" }).click();
  await panel.getByRole("switch", { name: "웹앱으로 공개" }).click();
  await expect(panel.getByRole("link", { name: "웹앱 열기" })).toBeVisible();
  const examples = panel.getByRole("list", { name: "호출 예시" });
  await expect(examples.getByText(/\/api\/v1\/chat-messages/).first()).toBeVisible();
  await expect(examples.getByText(/\/api\/v1\/conversations\?user=me/)).toBeVisible();

  await panel.getByRole("button", { name: "키 만들기" }).click();
  const keyDialog = page.getByRole("dialog", { name: "키 만들기" });
  await keyDialog.getByLabel("키 이름").fill("e2e 챗플로우 키");
  await keyDialog.getByRole("button", { name: "만들기" }).click();
  const created = page.getByRole("dialog", { name: "키를 만들었습니다" });
  await expect(created.getByText(/^sk-wf-/)).toBeVisible();
  await created.getByRole("button", { name: "닫기" }).first().click();
  const row = panel.getByRole("row", { name: /e2e 챗플로우 키/ });
  await row.getByRole("button", { name: "폐기" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "폐기" }).click();
  await expect(row.getByText("폐기됨")).toBeVisible();

  // 웹앱: 첫 메시지로 대화가 생기고 주소가 그 대화를 가리킨다. 새로 고쳐도 남는다.
  const { slug } = await (await page.request.get(`/api/console/workflows/${workflowId}`)).json();
  await page.goto(`/flows/${slug}`);
  await say(page, "웹앱 질문");
  await expect(page.getByText("가짜 응답: 웹앱 질문")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/flows/${slug}/c/[0-9a-f-]{36}$`));
  const sidebar = page.getByRole("navigation", { name: "대화 목록" });
  await expect(sidebar.getByRole("link", { name: /웹앱 질문/ })).toBeVisible();
  await page.reload();
  await expect(page.getByText("가짜 응답: 웹앱 질문")).toBeVisible();
  await expect(page.getByText("웹앱 질문", { exact: true }).first()).toBeVisible();
});

test("챗플로우: agent 노드의 셸 승인을 대화 안에서 하고, 기다리는 동안 새로 고쳐도 카드가 돌아온다", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const agentId = await createAgent(page, `e2e 챗플로우 셸 ${Date.now()}`);
  const created = await page.request.post("/api/console/workflows", {
    data: { name: `e2e 챗플로우 승인 ${Date.now()}`, kind: "chatflow" },
  });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  const draft = await (await page.request.get(`/api/console/workflows/${id}/draft`)).json();
  const saved = await page.request.put(`/api/console/workflows/${id}/draft`, {
    data: {
      graph: {
        nodes: [
          node("start", "start", 0, { fields: [] }),
          node(
            "agent_1",
            "agent",
            300,
            { agent_id: agentId, instruction: "{{ sys.query }}" },
            "셸 도우미",
          ),
          node("answer_1", "answer", 600, { text: "{{ agent_1.text }}" }),
        ],
        edges: [
          { id: "e1", source: "start", target: "agent_1" },
          { id: "e2", source: "agent_1", target: "answer_1" },
        ],
      },
      env_vars: [],
      conversation_vars: [],
      settings: draft.settings,
      updated_at: draft.updated_at,
    },
  });
  expect(saved.status()).toBe(200);
  expect((await saved.json()).issues).toEqual([]);

  await page.goto(`/workflows/${id}`);
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await say(panel, "셸을 써 줘");
  const approval = panel.getByRole("region", {
    name: "셸 도우미 노드의 에이전트가 승인을 기다립니다",
  });
  await expect(approval).toBeVisible();
  await expect(approval.locator("pre")).toHaveText(COMMAND);
  await expect(panel.getByRole("status", { name: "워크플로우 런 상태" })).toContainText(
    "입력 기다리는 중",
  );
  // 기다리는 동안에는 새 메시지를 보낼 수 없고 중지만 할 수 있다.
  await expect(panel.getByRole("textbox", { name: "메시지" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "중지" })).toBeVisible();

  // 새로 고치면 같은 대화를 열고 기다리는 런에 다시 붙어 카드가 돌아온다.
  await page.reload();
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await expect(approval).toBeVisible();

  await approval.getByRole("button", { name: "승인" }).click();
  await expect(panel.getByText(ANSWER)).toBeVisible();
  await expect(panel.getByRole("status", { name: "워크플로우 런 상태" })).toContainText("완료");
  await expect(approval).toHaveCount(0);
  await expect(panel.getByRole("textbox", { name: "메시지" })).toBeEnabled();
});
