import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import {
  expect as baseExpect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { setupFakeServer } from "./support.ts";

// DSL 화면(2단계 설계 5장, M18). 편집기에서 YAML로 내보내 목록에서 다시 가져오고, Dify DSL 가져오기 결과와
// 초안 덮어쓰기, 가져온 code 노드의 검토를 본다. 가짜 어댑터 서버(8502)를 쓴다.

const expect = baseExpect.configure({ timeout: 15_000 });
// 서버 테스트가 쓰는 DSL 픽스처
const FIXTURES = fileURLToPath(new URL("../../api/tests/fixtures/dsl/", import.meta.url));

setupFakeServer();

function node(id: string, type: string, x: number, config: Record<string, unknown>) {
  return { id, type, title: id, position: { x, y: 0 }, config };
}

// start → template → end와 비밀 환경 변수 하나
async function createFlow(request: APIRequestContext, name: string): Promise<string> {
  const created = await request.post("/api/console/workflows", { data: { name } });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  const draft = await (await request.get(`/api/console/workflows/${id}/draft`)).json();
  const saved = await request.put(`/api/console/workflows/${id}/draft`, {
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
          node("end_1", "end", 600, {
            outputs: [{ name: "greeting", var: "template_1.output" }],
          }),
        ],
        edges: [
          { id: "e1", source: "start", target: "template_1" },
          { id: "e2", source: "template_1", target: "end_1" },
        ],
      },
      env_vars: [{ name: "TOKEN", secret: true, value: "e2e-secret-value" }],
      conversation_vars: [],
      settings: draft.settings,
      updated_at: draft.updated_at,
    },
  });
  expect(saved.status()).toBe(200);
  return id;
}

async function openDslMenu(page: Page, item: string): Promise<void> {
  await page.getByRole("banner").getByRole("button", { name: "DSL 내보내기·가져오기" }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

async function chooseFile(
  page: Page,
  dialog: Locator,
  file: string | { name: string; mimeType: string; buffer: Buffer },
): Promise<void> {
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    dialog.getByRole("button", { name: "파일 고르기" }).click(),
  ]);
  await chooser.setFiles(file);
}

test("DSL: 편집기에서 내보낸 YAML을 목록에서 가져오면 노드 수가 같다", async ({ page }) => {
  const name = `e2e DSL ${Date.now()}`;
  const id = await createFlow(page.request, name);
  await page.goto(`/workflows/${id}`);
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  await expect(canvas.locator(".react-flow__node")).toHaveCount(3);

  // 비밀 값을 넣으면 경고 문구가 보이고, 내려받은 뒤 서버의 경고 헤더로 안내 토스트가 뜬다.
  await openDslMenu(page, "YAML로 내보내기");
  const exporter = page.getByRole("dialog", { name: "DSL 내보내기" });
  await exporter.getByRole("switch", { name: "비밀 값 포함" }).click();
  await expect(exporter.getByText(/비밀 환경 변수 값이 평문으로 파일에 들어갑니다/)).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    exporter.getByRole("button", { name: "내려받기" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(`${name}.yml`);
  const file = test.info().outputPath(`${name}.yml`);
  await download.saveAs(file);
  const yaml = await readFile(file, "utf8");
  expect(yaml).toContain("dsl_version: 1");
  expect(yaml).toContain("e2e-secret-value");
  await expect(
    page.getByText(`${name}.yml에 비밀 값이 평문으로 들어 있습니다. 안전한 곳에만 두세요.`, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(exporter).toHaveCount(0);

  // 목록에서 그 파일로 새 워크플로우를 만든다.
  await page.goto("/workflows");
  await page.getByRole("button", { name: "가져오기", exact: true }).click();
  const importer = page.getByRole("dialog", { name: "DSL 가져오기" });
  await chooseFile(page, importer, file);
  await expect(importer.getByText(`${name}.yml`)).toBeVisible();
  await importer.getByRole("button", { name: "가져오기", exact: true }).click();
  await expect(importer.getByText("가져왔습니다.")).toBeVisible();
  await expect(importer.getByText("우리 DSL")).toBeVisible();
  await expect(importer.getByText("노드 3개")).toBeVisible();
  await importer.getByRole("button", { name: "편집기 열기" }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]{36}$/);
  expect(page.url()).not.toContain(id);
  await expect(canvas.getByTestId("flow-node-template_1")).toBeVisible();
  await expect(canvas.locator(".react-flow__node")).toHaveCount(3);
  await expect(canvas.locator(".react-flow__edge")).toHaveCount(2);
});

test("DSL: Dify 챗플로우를 가져오면 옮기지 못한 노드가 보이고, 깨진 YAML과 2MB 초과는 이유를 알린다", async ({
  page,
}) => {
  await page.goto("/workflows");
  await page.getByRole("button", { name: "가져오기", exact: true }).click();
  const importer = page.getByRole("dialog", { name: "DSL 가져오기" });
  const submit = importer.getByRole("button", { name: "가져오기", exact: true });

  await importer.getByRole("tab", { name: "붙여 넣기" }).click();
  const box = importer.getByRole("textbox", { name: "YAML" });
  await box.fill("a: [1, 2");
  await submit.click();
  await expect(
    importer.getByText("YAML을 읽지 못했습니다. 들여쓰기·따옴표·괄호를 확인하세요."),
  ).toBeVisible();
  await expect(importer.getByText(/1행 9열/)).toBeVisible();

  // 2MB가 넘는 파일은 서버에 보내기 전에 막는다.
  await importer.getByRole("tab", { name: "파일" }).click();
  await chooseFile(page, importer, {
    name: "big.yaml",
    mimeType: "application/yaml",
    buffer: Buffer.alloc(2 * 1024 * 1024 + 1, "a"),
  });
  await submit.click();
  await expect(importer.getByText("파일이 2MB를 넘어 가져오지 못했습니다.")).toBeVisible();

  await importer.getByRole("tab", { name: "붙여 넣기" }).click();
  await box.fill(await readFile(`${FIXTURES}dify/chatflow_support.yaml`, "utf8"));
  await submit.click();
  await expect(importer.getByText("Dify DSL에서 변환")).toBeVisible();
  await expect(importer.getByText("노드 10개")).toBeVisible();
  // 도구 노드는 M19부터 내장 도구로 들어오고 지식 검색만 옮기지 못한다.
  const unsupported = importer.getByRole("region", { name: "옮기지 못한 노드 1개" });
  await expect(unsupported.getByText("n1730000000003")).toBeVisible();
  await expect(unsupported.getByText(/knowledge-retrieval/)).toBeVisible();
  const warnings = importer.getByRole("region", { name: /바꾸거나 버린 것/ });
  await expect(warnings.getByText(/폴백 목록의 첫 모델/).first()).toBeVisible();
  await expect(importer.getByRole("region", { name: /검증 결과/ })).toContainText(
    "오류 1 · 경고 0",
  );

  await importer.getByRole("button", { name: "편집기 열기" }).click();
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  await expect(canvas.getByTestId("flow-node-n1730000000003")).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 오류 1" })).toBeVisible();
});

test("DSL: 초안을 덮어쓰면 다시 불러오고, 가져온 code 노드는 검토 완료를 누를 때까지 배지가 붙는다", async ({
  page,
}) => {
  const created = await page.request.post("/api/console/workflows", {
    data: { name: `e2e DSL 덮어쓰기 ${Date.now()}` },
  });
  expect(created.status()).toBe(201);
  const { id } = (await created.json()) as { id: string };
  await page.goto(`/workflows/${id}`);
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await expect(canvas.locator(".react-flow__node")).toHaveCount(2);

  await openDslMenu(page, "가져와서 이 초안 덮어쓰기");
  const importer = page.getByRole("dialog", { name: "가져와서 이 초안 덮어쓰기" });
  await chooseFile(page, importer, `${FIXTURES}ours/workflow_iteration.yaml`);
  await importer.getByRole("button", { name: "이 초안 덮어쓰기" }).click();
  const confirm = page.getByRole("alertdialog", { name: "초안을 덮어쓸까요?" });
  await confirm.getByRole("button", { name: "덮어쓰기" }).click();
  await expect(importer.getByText("초안을 덮어쓰고 다시 불러왔습니다.")).toBeVisible();
  await expect(importer.getByText("노드 6개")).toBeVisible();
  // 닫기는 제목 옆 X와 아래 단추 둘이다.
  await importer.getByRole("button", { name: "닫기" }).last().click();

  await expect(canvas.locator(".react-flow__node")).toHaveCount(6);
  const card = canvas.getByTestId("flow-node-split");
  await expect(card.getByText("가져온 코드")).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 오류 1" })).toBeVisible();

  // 검증 오류는 노드의 이슈 목록과 코드 칸 아래에 함께 보인다.
  await card.click();
  await expect(panel.getByText("가져온 코드입니다")).toBeVisible();
  await expect(
    panel.getByRole("region", { name: "이 노드의 검증 결과" }).getByText(/가져온 코드다/),
  ).toBeVisible();
  await expect(panel.getByRole("alert").filter({ hasText: "가져온 코드다" })).toBeVisible();

  await panel.getByRole("button", { name: "검토 완료" }).click();
  await expect(card.getByText("가져온 코드")).toHaveCount(0);
  await expect(panel.getByText("가져온 코드입니다")).toHaveCount(0);
  // 자동 저장이 플래그를 지운 초안을 올리면 서버 검증 오류도 풀린다.
  await expect(page.getByRole("button", { name: "검증 결과: 문제 없음" })).toBeVisible();
  const draft = await (await page.request.get(`/api/console/workflows/${id}/draft`)).json();
  const split = (draft.graph.nodes as { id: string; ui: Record<string, unknown> }[]).find(
    (item) => item.id === "split",
  );
  expect(split?.ui).not.toHaveProperty("imported_code");
});
