import {
  expect as baseExpect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { createAgent, preview, send, setupFakeServer } from "./support.ts";

/*
 * 지식 베이스를 쓰는 화면(3단계 설계 16장 M26). 가짜 임베더로 KB에 글 문서를 넣고, 캔버스에서
 * start → 지식 검색 → LLM(컨텍스트) → 답변 챗플로우를 만들어 미리보기 대화와 발행한 웹앱에 인용 칩이 붙는지,
 * 웹앱 설정의 인용 스위치를 끄면 사라지는지 본다. 에이전트 빌더 지식 탭에서 KB를 붙이면 도구 탭에
 * search_knowledge가 보인다. 가짜 어댑터 서버(8502)를 쓴다.
 *
 * 에이전트가 search_knowledge를 부르는 시험은 가짜 어댑터 각본이 있어야 해서 그 각본으로 새로 띄운 서버에서만
 * 돈다(각본은 서버 전체가 순서대로 한 번만 쓴다). 다른 시험은 기본 서버에서 돌고 각본 서버에서는 건너뛴다.
 *   SERVER_PORT=8502 DATA_DIR=C:\wf-scratch LLM_FAKE=1 \
 *     LLM_FAKE_SCRIPT=apps/web/e2e/fixtures/knowledge_agent_search.json uv run platform serve
 *   E2E_FAKE_SCRIPT=knowledge_agent_search corepack pnpm --dir apps/web e2e knowledge
 */

const expect = baseExpect.configure({ timeout: 15_000 });
const SCRIPT = process.env.E2E_FAKE_SCRIPT ?? "";

setupFakeServer();

const DOCUMENT = "휴가 규정";
const DOCUMENT_TEXT =
  "연차 휴가는 다음 해로 5일까지 이월할 수 있다. 이월한 연차는 다음 해 3월 말까지 써야 하고 " +
  "그 뒤에는 사라진다.";

// 가짜 임베더로 KB를 만들고 글 문서 하나를 넣어 색인이 끝나기를 기다린다.
async function knowledgeBase(request: APIRequestContext, name: string): Promise<string> {
  const created = await request.post("/api/console/knowledge", {
    data: {
      name,
      description: "e2e 지식 베이스",
      doc_form: "text_model",
      indexing_technique: "high_quality",
      search_while_indexing: true,
      data_policy: { key_tier: "free", acknowledged: true },
    },
  });
  expect(created.status()).toBe(201);
  const kbId = (await created.json()).id as string;
  const uploaded = await request.post(`/api/console/knowledge/${kbId}/documents`, {
    data: { name: DOCUMENT, text: DOCUMENT_TEXT },
  });
  expect(uploaded.ok()).toBeTruthy();
  await expect
    .poll(
      async () => {
        const list = await (await request.get(`/api/console/knowledge/${kbId}/documents`)).json();
        return (list.items as { indexing_status: string }[]).map((item) => item.indexing_status);
      },
      { timeout: 30_000 },
    )
    .toEqual(["completed"]);
  return kbId;
}

async function say(scope: Locator | Page, text: string): Promise<void> {
  const box = scope.getByRole("textbox", { name: "메시지" });
  await box.fill(text);
  await box.press("Enter");
}

// 노드를 골라 둔 채 팔레트를 누르면 그 뒤에 이어 붙는다.
async function addAfter(page: Page, nodeId: string, paletteLabel: string): Promise<void> {
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  await page.getByRole("button", { name: "화면에 맞추기" }).click();
  await canvas.getByTestId(`flow-node-${nodeId}`).click({ position: { x: 40, y: 16 } });
  await page
    .getByRole("complementary", { name: "노드" })
    .getByRole("button", { name: `${paletteLabel} 추가`, exact: true })
    .click();
}

test("지식: 캔버스로 만든 지식 챗플로우의 미리보기·웹앱에 인용 칩이 붙고 스위치로 끄며, 에이전트 지식 탭이 search_knowledge를 붙인다", async ({
  page,
}) => {
  test.skip(SCRIPT !== "", "각본 서버에서는 에이전트 검색 시험만 돈다");
  test.setTimeout(240_000);
  const stamp = Date.now();
  const kbName = `e2e 지식 ${stamp}`;
  const kbId = await knowledgeBase(page.request, kbName);

  // 빈 챗플로우는 start → answer_1이다. 답변을 지우고 start 뒤에 지식 검색 → LLM → 답변을 잇는다.
  const created = await page.request.post("/api/console/workflows", {
    data: { name: `e2e 지식 챗플로우 ${stamp}`, kind: "chatflow" },
  });
  expect(created.status()).toBe(201);
  const workflow = (await created.json()) as { id: string; slug: string };
  await page.goto(`/workflows/${workflow.id}`);
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await canvas.getByTestId("flow-node-answer_1").click();
  await panel.getByRole("button", { name: "노드 지우기" }).click();
  await expect(canvas.getByTestId("flow-node-answer_1")).toHaveCount(0);

  // 지식 검색: 팔레트의 "지식" 분류에 있고, 챗플로우는 sys.query를 질의로 쓴다.
  await expect(
    page.getByRole("complementary", { name: "노드" }).getByRole("heading", { name: "지식" }),
  ).toBeVisible();
  await addAfter(page, "start", "지식 검색");
  await expect(canvas.getByTestId("flow-node-knowledge_retrieval_1")).toBeVisible();
  await panel.getByRole("checkbox", { name: new RegExp(kbName) }).click();
  await expect(panel.getByText("1/5개 고름")).toBeVisible();
  await expect(canvas.getByTestId("flow-node-knowledge_retrieval_1")).toContainText(kbName);
  await expect(panel.getByRole("button", { name: "질의 변수" })).toContainText("query");

  // LLM: 컨텍스트로 지식 검색 결과를 쓴다.
  await addAfter(page, "knowledge_retrieval_1", "LLM");
  await expect(canvas.getByTestId("flow-node-llm_1")).toBeVisible();
  await panel.getByRole("switch", { name: "검색 결과를 컨텍스트로 쓰기" }).click();
  await panel.getByRole("button", { name: "컨텍스트 변수" }).click();
  await page
    .getByRole("group", { name: "지식 검색", exact: true })
    .getByRole("button", { name: /^result(?!_text)/ })
    .click();
  await expect(panel.getByRole("button", { name: "컨텍스트 변수" })).toContainText("result");

  // 답변: {{를 쳐서 변수 목록에서 llm_1.text를 넣는다.
  await addAfter(page, "llm_1", "답변");
  await expect(canvas.getByTestId("flow-node-answer_1")).toBeVisible();
  await panel.getByRole("textbox", { name: "답변" }).click();
  await page.keyboard.type("{{");
  await page.getByRole("searchbox", { name: "변수 찾기" }).fill("llm_1.text");
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("textbox", { name: "답변" })).toContainText("{{ llm_1.text }}");
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: /저장 상태: 저장됨/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 문제 없음" })).toBeVisible();
  const draft = await (
    await page.request.get(`/api/console/workflows/${workflow.id}/draft`)
  ).json();
  const nodes = draft.graph.nodes as { id: string; config: Record<string, unknown> }[];
  expect(nodes.find((node) => node.id === "knowledge_retrieval_1")?.config).toMatchObject({
    kb_ids: [kbId],
    query_var: "sys.query",
    retrieval: null,
  });
  expect(nodes.find((node) => node.id === "llm_1")?.config.context).toEqual({
    var: "knowledge_retrieval_1.result",
  });

  // 미리보기 대화: 답 아래 인용 칩, 팝오버에 문서·지식 베이스·내용과 청크 화면 링크.
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await say(panel, "연차 이월 규정");
  await expect(panel.getByText("가짜 응답: 연차 이월 규정")).toBeVisible();
  const chip = panel.getByRole("button", { name: `인용 1: ${DOCUMENT}` });
  await expect(chip).toBeVisible();
  await chip.click();
  const popover = page.getByRole("dialog", { name: "인용 1 내용" });
  await expect(popover.getByText(new RegExp(`${kbName} · 청크 #1`))).toBeVisible();
  await expect(popover.getByText(/5일까지 이월할 수 있다/)).toBeVisible();
  await expect(popover.getByRole("link", { name: "청크 화면에서 전문 보기" })).toHaveAttribute(
    "href",
    new RegExp(`/knowledge/${kbId}/documents/[0-9a-f-]{36}$`),
  );
  await page.keyboard.press("Escape");

  // 실행 과정: 지식 검색 노드 카드에 결과 목록(순위·점수·문서).
  await panel.getByRole("tab", { name: "실행 과정" }).click();
  const retrievalRun = panel.getByTestId("node-run-knowledge_retrieval_1");
  await expect(retrievalRun).toHaveAttribute("data-status", "succeeded");
  await retrievalRun.getByRole("button").first().click();
  const results = retrievalRun.getByRole("list", { name: "검색 결과" });
  await expect(results.getByText("#1", { exact: true })).toBeVisible();
  await expect(results.getByRole("link", { name: DOCUMENT })).toBeVisible();
  await panel.getByRole("tab", { name: "대화", exact: true }).click();

  // 발행하고 웹앱을 열면 같은 칩이 붙는다(웹앱 팝오버에는 콘솔 링크가 없다).
  await panel.getByRole("tab", { name: "발행" }).click();
  await panel.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v1을(를) 발행했습니다.", { exact: true })).toBeVisible();
  const enabled = await page.request.patch(`/api/console/workflows/${workflow.id}`, {
    data: { webapp_enabled: true },
  });
  expect(enabled.ok()).toBeTruthy();
  await page.goto(`/flows/${workflow.slug}`);
  await say(page, "연차 이월 규정");
  await expect(page.getByText("가짜 응답: 연차 이월 규정")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/flows/${workflow.slug}/c/[0-9a-f-]{36}$`));
  const conversationUrl = page.url();
  await page.getByRole("button", { name: `인용 1: ${DOCUMENT}` }).click();
  await expect(page.getByRole("dialog", { name: "인용 1 내용" })).toBeVisible();
  await expect(page.getByRole("link", { name: "청크 화면에서 전문 보기" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // 웹앱 설정의 인용 스위치를 끄고 다시 발행하면 웹앱에서 칩이 사라진다(API 응답에는 그대로 있다).
  await page.goto(`/workflows/${workflow.id}`);
  await panel.getByRole("tab", { name: "설정" }).click();
  const citations = panel.getByRole("switch", { name: "인용 보이기" });
  await expect(citations).toHaveAttribute("aria-checked", "true");
  await citations.click();
  await expect(citations).toHaveAttribute("aria-checked", "false");
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: /저장 상태: 저장됨/ })).toBeVisible();
  await panel.getByRole("tab", { name: "발행" }).click();
  await panel.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v2을(를) 발행했습니다.", { exact: true })).toBeVisible();
  await page.goto(conversationUrl);
  await expect(page.getByText("가짜 응답: 연차 이월 규정")).toBeVisible();
  await expect(page.getByRole("list", { name: "인용" })).toHaveCount(0);
  const cid = conversationUrl.split("/").pop()!;
  const messages = await (
    await page.request.get(`/api/web/wf-conversations/${cid}/messages`)
  ).json();
  const answer = (messages.items as { role: string; retriever_resources: unknown[] }[]).find(
    (item) => item.role === "assistant",
  );
  expect(answer?.retriever_resources).toHaveLength(1);

  // 에이전트 빌더 지식 탭: KB를 붙이고 저장하면 도구 탭에 search_knowledge가 자동 도구로 보인다.
  const agentId = await createAgent(page, `e2e 지식 에이전트 ${stamp}`);
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "지식" }).click();
  await page.getByRole("checkbox", { name: new RegExp(kbName) }).click();
  await expect(page.getByText("search_knowledge 도구가 붙습니다")).toBeVisible();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "도구와 스킬" }).click();
  await expect(page.getByText(/지식 베이스 1개를 검색합니다 \(search_knowledge\)/)).toBeVisible();
  const agentDraft = await (await page.request.get(`/api/console/agents/${agentId}/draft`)).json();
  expect(agentDraft.knowledge).toEqual({ kb_ids: [kbId], retrieval_model: null });
});

test("지식: 에이전트 미리보기에서 search_knowledge 도구 카드에 결과 요약이, 답 아래에 인용 칩이 보인다", async ({
  page,
}) => {
  test.skip(
    SCRIPT !== "knowledge_agent_search",
    "LLM_FAKE_SCRIPT=knowledge_agent_search.json으로 띄운 서버에서만 돈다",
  );
  test.setTimeout(120_000);
  const stamp = Date.now();
  const kbName = `e2e 에이전트 지식 ${stamp}`;
  const kbId = await knowledgeBase(page.request, kbName);
  const agentId = await createAgent(page, `e2e 검색 에이전트 ${stamp}`);

  // 지식 탭에서 붙이고 저장한다.
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "지식" }).click();
  await page.getByRole("checkbox", { name: new RegExp(kbName) }).click();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();

  // 각본: search_knowledge("연차 이월") → "… [1]." 답. 승인 등급이 read라 바로 실행된다.
  await send(page, "연차 이월 규정을 알려 줘");
  const panel = preview(page);
  await expect(panel.getByText("연차는 다음 해로 5일까지 넘길 수 있습니다 [1].")).toBeVisible();
  await expect(panel.getByText("search_knowledge", { exact: true })).toBeVisible();
  await expect(panel.getByText(`결과 1개(문서 1개) · 1위 ${DOCUMENT}`)).toBeVisible();
  // 도구 카드와 답 아래에 같은 인용 칩이 있다. 런이 끝나면 답 아래 칩은 메시지 저장본으로 바뀐다.
  await expect(panel.getByRole("button", { name: `인용 1: ${DOCUMENT}` })).toHaveCount(2);
  await panel
    .getByRole("button", { name: `인용 1: ${DOCUMENT}` })
    .last()
    .click();
  const popover = page.getByRole("dialog", { name: "인용 1 내용" });
  await expect(popover.getByText(/5일까지 이월할 수 있다/)).toBeVisible();
  await expect(popover.getByRole("link", { name: "청크 화면에서 전문 보기" })).toHaveAttribute(
    "href",
    new RegExp(`/knowledge/${kbId}/documents/`),
  );
  await page.keyboard.press("Escape");

  // 새로 고치면 기록에서 다시 그린다. 도구 카드는 결과 글로 요약하고 칩은 마지막 답에만 붙는다.
  await page.reload();
  await expect(panel.getByText("연차는 다음 해로 5일까지 넘길 수 있습니다 [1].")).toBeVisible();
  await expect(panel.getByText(`결과 1개(문서 1개) · 1위 ${DOCUMENT}`)).toBeVisible();
  await expect(panel.getByRole("button", { name: `인용 1: ${DOCUMENT}` })).toHaveCount(1);
});
