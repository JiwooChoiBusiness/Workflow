import { expect as baseExpect, test, type APIRequestContext } from "@playwright/test";
import { setupFakeServer } from "./support.ts";

// 실행 패널(2단계 설계 7.3, M16). 캔버스에서 start → template → llm → end를 실행하면 노드 카드에 상태가
// 붙고 결과가 패널에 보이며, 발행해 웹앱(/flows/:slug)에서도 같은 흐름이 돈다. 가짜 어댑터 서버(8502)를 쓴다.

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

function node(id: string, type: string, x: number, config: Record<string, unknown>) {
  return { id, type, title: id, position: { x, y: 0 }, config };
}

async function createFlow(
  request: APIRequestContext,
  name: string,
): Promise<{ id: string; slug: string }> {
  const created = await request.post("/api/console/workflows", { data: { name } });
  expect(created.status()).toBe(201);
  const workflow = (await created.json()) as { id: string; slug: string };
  const draft = await (await request.get(`/api/console/workflows/${workflow.id}/draft`)).json();
  const saved = await request.put(`/api/console/workflows/${workflow.id}/draft`, {
    data: {
      graph: {
        nodes: [
          node("start", "start", 0, {
            fields: [{ name: "q", type: "string", label: "질문", required: true }],
          }),
          node("template_1", "template", 300, {
            template: "안녕, {{ q }}",
            vars: [{ name: "q", var: "start.q" }],
          }),
          node("llm_1", "llm", 600, {
            prompt: [{ role: "user", text: "{{ template_1.output }}" }],
          }),
          node("end_1", "end", 900, {
            outputs: [
              { name: "greeting", var: "template_1.output" },
              { name: "answer", var: "llm_1.text" },
            ],
          }),
        ],
        edges: [
          { id: "e1", source: "start", target: "template_1" },
          { id: "e2", source: "template_1", target: "llm_1" },
          { id: "e3", source: "llm_1", target: "end_1" },
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
  return workflow;
}

test("실행 패널: 캔버스에서 실행하면 노드 카드에 상태가 붙고 결과가 보이며 웹앱에서도 돈다", async ({
  page,
}) => {
  const workflow = await createFlow(page.request, `e2e 실행 ${Date.now()}`);
  await page.goto(`/workflows/${workflow.id}`);
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await expect(canvas.getByTestId("flow-node-llm_1")).toBeVisible();

  // 상단 실행 단추는 실행 탭을 연다. 입력 칸이 있어 바로 돌지 않는다.
  await page.getByRole("banner").getByRole("button", { name: "실행", exact: true }).click();
  await expect(panel.getByRole("tab", { name: "실행" })).toHaveAttribute("aria-selected", "true");
  await panel.getByRole("button", { name: "실행", exact: true }).click();
  await expect(panel.getByText("필수 입력입니다.")).toBeVisible();

  await panel.getByLabel("질문").fill("세계");
  await panel.getByRole("button", { name: "실행", exact: true }).click();
  await expect(panel.getByRole("status", { name: "워크플로우 런 상태" })).toContainText("완료");

  // 캔버스 카드마다 실행 상태 점이 붙는다.
  for (const id of ["start", "template_1", "llm_1", "end_1"]) {
    await expect(
      canvas.getByTestId(`flow-node-${id}`).locator("[data-run-status]"),
    ).toHaveAttribute("data-run-status", "succeeded");
  }
  const result = panel.getByRole("region", { name: "실행 결과" });
  await expect(result.getByText("안녕, 세계").first()).toBeVisible();
  await expect(result.getByText("가짜 응답: 안녕, 세계")).toBeVisible();
  await expect(panel.getByTestId("node-run-llm_1")).toHaveAttribute("data-status", "succeeded");

  // 카드의 상태 점을 누르면 실행 패널에서 그 노드 런이 펼쳐지고 전문(입력·출력)을 받아 온다.
  await canvas.getByTestId("flow-node-template_1").locator("[data-run-status]").click();
  const card = panel.getByTestId("node-run-template_1");
  await expect(card.getByText("입력", { exact: true })).toBeVisible();
  await expect(card.getByText('"안녕, 세계"')).toBeVisible();
  await expect(page).not.toHaveURL(/node=/);

  // 실행 기록 화면에도 남는다.
  await panel.getByRole("link", { name: "이 런의 기록" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${workflow.id}/logs/[0-9a-f-]{36}$`));
  await expect(page.getByTestId("node-run-llm_1")).toHaveAttribute("data-status", "succeeded");

  // 발행하고 웹앱으로 공개하면 /flows/:slug에서 같은 입력 폼으로 돈다.
  const published = await page.request.post(`/api/console/workflows/${workflow.id}/publish`, {
    data: {},
  });
  expect(published.status()).toBe(201);
  const enabled = await page.request.patch(`/api/console/workflows/${workflow.id}`, {
    data: { webapp_enabled: true },
  });
  expect(enabled.ok()).toBeTruthy();
  await page.goto(`/flows/${workflow.slug}`);
  await page.getByLabel("질문").fill("웹앱");
  await page.getByRole("button", { name: "실행", exact: true }).click();
  const webResult = page.getByRole("region", { name: "실행 결과" });
  await expect(webResult.getByText("가짜 응답: 안녕, 웹앱")).toBeVisible();
  await expect(page).toHaveURL(/\?run=[0-9a-f-]{36}$/);
  await expect(
    page.getByRole("complementary", { name: "내 실행 기록" }).getByText("웹앱"),
  ).toBeVisible();
});
