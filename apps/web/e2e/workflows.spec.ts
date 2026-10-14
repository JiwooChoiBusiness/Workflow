import { expect as baseExpect, test, type Locator, type Page } from "@playwright/test";
import { setupFakeServer } from "./support.ts";

// 캔버스 편집기(2단계 설계 7.3, M15). 목록에서 만들고 노드를 이어 저장한 뒤 새로 고쳐도 남고 발행된다.
// 가짜 어댑터 서버(8502)를 쓴다. 실행은 workflow-run.spec.ts가 본다.

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

function handle(page: Page, node: string, id: string): Locator {
  return page.locator(`.react-flow__handle[data-nodeid="${node}"][data-handleid="${id}"]`);
}

// ReactFlow 핸들 연결은 마우스 끌기로 한다.
async function connect(page: Page, from: Locator, to: Locator): Promise<void> {
  const a = await from.boundingBox();
  const b = await to.boundingBox();
  if (!a || !b) throw new Error("핸들이 화면에 없다");
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 12 });
  await page.mouse.up();
}

test("워크플로우: 목록에서 만들고 시작·템플릿·끝을 이어 저장하면 새로 고쳐도 남고 발행된다", async ({
  page,
}) => {
  const name = `e2e 워크플로우 ${Date.now()}`;
  await page.goto("/workflows");
  await expect(page.getByRole("heading", { level: 1, name: "워크플로우" })).toBeVisible();
  await page.getByRole("button", { name: "새 워크플로우" }).first().click();
  const dialog = page.getByRole("dialog", { name: "새 워크플로우" });
  await dialog.getByLabel("이름").fill(name);
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]{36}$/);

  // 빈 워크플로우는 start → end_1이다. 편집기에서는 내비게이션 레일이 접혀 있다.
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  const palette = page.getByRole("complementary", { name: "노드" });
  await expect(canvas.getByTestId("flow-node-start")).toBeVisible();
  await expect(canvas.getByTestId("flow-node-end_1")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "주 메뉴" })).toHaveCount(0);

  // 시작 노드에 입력 필드 q
  await canvas.getByTestId("flow-node-start").click();
  await expect(page).toHaveURL(/\?node=start$/);
  await panel.getByRole("button", { name: "필드 추가" }).click();
  await panel.getByLabel("변수 이름").fill("q");

  // 시작을 골라 둔 채 템플릿을 누르면 그 뒤에 이어 붙는다. 템플릿 → 끝은 핸들을 끌어 잇는다.
  await palette.getByRole("button", { name: "템플릿 추가" }).click();
  await expect(canvas.getByTestId("flow-node-template_1")).toBeVisible();
  await expect(canvas.locator(".react-flow__edge")).toHaveCount(2);
  await connect(page, handle(page, "template_1", "out"), handle(page, "end_1", "in"));
  await expect(canvas.locator(".react-flow__edge")).toHaveCount(3);

  // 템플릿: 변수 q ← start.q
  await canvas.getByTestId("flow-node-template_1").click();
  await panel.getByRole("button", { name: "변수 추가" }).click();
  await panel.getByLabel("변수 1번째 줄 이름").fill("q");
  await panel.getByRole("button", { name: "변수 1번째 줄 변수" }).click();
  await page.getByRole("group", { name: "시작" }).getByRole("button", { name: /^q/ }).click();
  await expect(panel.getByRole("button", { name: "변수 1번째 줄 변수" })).toContainText("q");

  // 끝: 출력 result ← template_1.output
  await canvas.getByTestId("flow-node-end_1").click();
  await panel.getByRole("button", { name: "출력 추가" }).click();
  await panel.getByLabel("출력 1번째 줄 이름").fill("result");
  await panel.getByRole("button", { name: "출력 1번째 줄 변수" }).click();
  await page
    .getByRole("group", { name: "템플릿" })
    .getByRole("button", { name: /^output/ })
    .click();

  // Ctrl+S로 바로 저장하면 서버 검증 결과가 함께 온다.
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: /저장 상태: 저장됨/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 문제 없음" })).toBeVisible();

  // 새로 고쳐도 같은 그래프
  await page.reload();
  await expect(canvas.getByTestId("flow-node-template_1")).toBeVisible();
  await expect(canvas.locator(".react-flow__node")).toHaveCount(3);
  await expect(canvas.locator(".react-flow__edge")).toHaveCount(3);
  await canvas.getByTestId("flow-node-end_1").click();
  await expect(panel.getByLabel("출력 1번째 줄 이름")).toHaveValue("result");

  // 발행
  await page.getByRole("banner").getByRole("button", { name: "발행" }).click();
  await panel.getByLabel("변경 메모").fill("e2e 첫 발행");
  await panel.getByRole("button", { name: "발행" }).click();
  // 토스트 글은 화면 읽기 프로그램용 알림 영역에도 한 번 더 나온다.
  await expect(page.getByText("v1을(를) 발행했습니다.", { exact: true })).toBeVisible();
  await expect(panel.getByText("e2e 첫 발행")).toBeVisible();
  await expect(panel.getByText("현재 발행")).toBeVisible();
});
