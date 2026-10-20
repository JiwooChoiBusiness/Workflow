import { expect as baseExpect, test, type Locator, type Page } from "@playwright/test";
import { setupFakeServer } from "./support.ts";

// 2차 노드(2단계 설계 2.4, M19). 캔버스에서 도구(JSON 읽기) → 리스트 연산 → 도구(현재 시각) → 루프(안에 변수
// 쓰기) → 사람 입력 → 끝을 만들어 실행하면 사람 입력 폼에서 멈추고, 실행 탭에서 폼을 보내면 같은 런이 끝난다.
// 발행한 웹앱에서도 같은 폼을 받는다. 가짜 어댑터 서버를 쓴다(LLM은 쓰지 않는다).

const expect = baseExpect.configure({ timeout: 15_000 });

setupFakeServer();

async function choose(page: Page, combobox: Locator, option: string | RegExp): Promise<void> {
  await combobox.click();
  await page.getByRole("option", { name: option, exact: typeof option === "string" }).click();
}

// 변수 선택기를 열어 group(노드 이름) 안의 변수를 고른다.
async function pickVar(page: Page, trigger: Locator, group: string, name: RegExp): Promise<void> {
  await trigger.click();
  await page.getByRole("group", { name: group, exact: true }).getByRole("button", { name }).click();
}

// 노드를 골라 둔 채 팔레트를 누르면 그 뒤(같은 틀 안)에 이어 붙는다.
async function addAfter(page: Page, nodeId: string, paletteLabel: string): Promise<void> {
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  await page.getByRole("button", { name: "화면에 맞추기" }).click();
  await canvas.getByTestId(`flow-node-${nodeId}`).click({ position: { x: 40, y: 16 } });
  await page
    .getByRole("complementary", { name: "노드" })
    .getByRole("button", { name: `${paletteLabel} 추가`, exact: true })
    .click();
}

test("2차 노드: 캔버스에서 만든 루프·리스트 연산·도구 흐름이 사람 입력 폼에서 멈췄다가 이어서 끝나고 웹앱에서도 같은 폼을 받는다", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const created = await page.request.post("/api/console/workflows", {
    data: { name: `e2e 2차 노드 ${Date.now()}` },
  });
  expect(created.status()).toBe(201);
  const workflow = (await created.json()) as { id: string; slug: string };
  await page.goto(`/workflows/${workflow.id}`);
  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await expect(canvas.getByTestId("flow-node-end_1")).toBeVisible();

  // 빈 그래프의 끝은 지우고 마지막에 다시 붙인다. 시작에는 목록 JSON을 받는 칸을 둔다.
  await canvas.getByTestId("flow-node-end_1").click();
  await panel.getByRole("button", { name: "노드 지우기" }).click();
  await canvas.getByTestId("flow-node-start").click();
  await panel.getByRole("button", { name: "필드 추가" }).click();
  await panel.getByLabel("변수 이름").fill("items");

  // 도구: JSON 읽기(글 ← start.items)
  await addAfter(page, "start", "도구");
  await expect(canvas.getByTestId("flow-node-tool_1")).toBeVisible();
  await choose(page, panel.getByRole("combobox", { name: "도구", exact: true }), /JSON 읽기/);
  await choose(page, panel.getByRole("combobox", { name: "JSON 글 값 방식" }), "변수");
  await pickVar(page, panel.getByRole("button", { name: "JSON 글" }), "시작", /^items/);
  await expect(canvas.getByTestId("flow-node-tool_1")).toContainText("json_parse");

  // 리스트 연산: 1보다 큰 원소를 큰 순서로 셋까지, 첫 원소를 item으로
  await addAfter(page, "tool_1", "리스트 연산");
  await pickVar(page, panel.getByRole("button", { name: "입력 배열" }), "도구", /^value/);
  await panel.getByRole("switch", { name: "거르기" }).click();
  await panel.getByRole("button", { name: "조건 추가" }).click();
  await choose(page, panel.getByRole("combobox", { name: "연산자" }), /보다 큼/);
  await panel.getByRole("textbox", { name: "비교 값" }).fill("1");
  await panel.getByRole("switch", { name: "정렬" }).click();
  await choose(page, panel.getByRole("combobox", { name: "정렬 방향" }), "내림차순");
  await panel.getByRole("switch", { name: "자르기" }).click();
  await panel.getByLabel("남길 수").fill("3");
  await panel.getByRole("switch", { name: "하나 꺼내기" }).click();

  // 도구: 현재 시각(매개변수는 기본값)
  await addAfter(page, "list_operator_1", "도구");
  await choose(page, panel.getByRole("combobox", { name: "도구", exact: true }), /현재 시각/);

  // 루프: 반복 변수 n(0에서), n ≥ 3이면 멈춤, 최대 5회
  await addAfter(page, "tool_2", "루프");
  await expect(canvas.getByTestId("flow-node-loop_start_1")).toBeVisible();
  await panel.getByRole("button", { name: "변수 추가" }).click();
  await panel.getByRole("textbox", { name: "이름", exact: true }).fill("n");
  await choose(page, panel.getByRole("combobox", { name: "타입", exact: true }), "number");
  await panel.getByRole("textbox", { name: "n 고정값" }).fill("0");
  await panel.getByRole("button", { name: "조건 추가" }).click();
  await pickVar(
    page,
    panel.getByRole("button", { name: "종료 조건 1 변수" }),
    "반복 변수 · 루프",
    /^n/,
  );
  await choose(page, panel.getByRole("combobox", { name: "연산자" }), /이상/);
  await panel.getByRole("textbox", { name: "비교 값" }).fill("3");
  await panel.getByLabel("최대 반복 수").fill("5");
  await expect(canvas.getByTestId("flow-node-loop_1")).toContainText("최대 5회");

  // 루프 안: 시작 자리 뒤에 변수 쓰기(n += 1). 워크플로우에서는 루프 안에서만 놓인다.
  await addAfter(page, "loop_start_1", "변수 쓰기");
  await expect(canvas.getByTestId("flow-node-variable_assigner_1")).toBeVisible();
  await panel.getByRole("button", { name: "항목 추가" }).click();
  await choose(page, panel.getByRole("combobox", { name: "대상 변수" }), "loop_1.n");
  await choose(page, panel.getByRole("combobox", { name: "방법" }), "더하기(+=)");
  await choose(page, panel.getByRole("combobox", { name: "넣을 값" }), "고정값");
  await panel.getByRole("textbox", { name: "1번째 줄 고정값" }).fill("1");

  // 사람 입력: 안내문과 기본 필드(답)
  await addAfter(page, "loop_1", "사람 입력");
  await panel.getByRole("textbox", { name: "안내문" }).click();
  await page.keyboard.type("목록을 확인하고 답하세요");

  // 끝: 꺼낸 원소, 루프 횟수, 사람의 답
  await addAfter(page, "human_input_1", "끝");
  for (const [index, name, group, output] of [
    [1, "first", "리스트 연산", /^item/],
    [2, "rounds", "루프", /^loop_count/],
    [3, "reply", "사람 입력", /^reply/],
  ] as const) {
    await panel.getByRole("button", { name: "출력 추가" }).click();
    await panel.getByLabel(`출력 ${index}번째 줄 이름`).fill(name);
    await pickVar(
      page,
      panel.getByRole("button", { name: `출력 ${index}번째 줄 변수` }),
      group,
      output,
    );
  }
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: /저장 상태: 저장됨/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 문제 없음" })).toBeVisible();

  // 실행하면 사람 입력에서 멈춘다. 폼을 보내면 같은 런이 이어서 끝난다.
  await page.getByRole("banner").getByRole("button", { name: "실행", exact: true }).click();
  await panel.getByLabel("items").fill("[5, 1, 4, 2, 3]");
  await panel.getByRole("button", { name: "실행", exact: true }).click();
  const status = panel.getByRole("status", { name: "워크플로우 런 상태" });
  const form = panel.getByRole("region", { name: "사람 입력 노드가 입력을 기다립니다" });
  await expect(form).toBeVisible();
  await expect(status).toContainText("입력 기다리는 중");
  await expect(form.getByText("목록을 확인하고 답하세요")).toBeVisible();
  await form.getByRole("button", { name: "보내기" }).click();
  await expect(form.getByText("필수 입력입니다.")).toBeVisible();
  await form.getByRole("textbox", { name: "답" }).fill("확인함");
  await form.getByRole("button", { name: "보내기" }).click();
  await expect(status).toContainText("완료");
  await expect(form).toHaveCount(0);
  const result = panel.getByRole("region", { name: "실행 결과" });
  await expect(result.getByText("확인함")).toBeVisible();
  for (const id of ["tool_1", "list_operator_1", "tool_2", "loop_1", "human_input_1", "end_1"]) {
    await expect(panel.getByTestId(`node-run-${id}`)).toHaveAttribute("data-status", "succeeded");
  }
  // 루프는 반복 순번별로 접힌다.
  const loopRun = panel.getByTestId("node-run-loop_1");
  await loopRun.getByRole("button").first().click();
  await expect(loopRun.getByText("3회 · 최대 5회")).toBeVisible();
  await expect(loopRun.getByRole("button", { name: /회차/ })).toHaveCount(3);

  // 발행해 웹앱으로 열면 같은 폼을 받는다.
  const published = await page.request.post(`/api/console/workflows/${workflow.id}/publish`, {
    data: {},
  });
  expect(published.status()).toBe(201);
  const enabled = await page.request.patch(`/api/console/workflows/${workflow.id}`, {
    data: { webapp_enabled: true },
  });
  expect(enabled.ok()).toBeTruthy();
  await page.goto(`/flows/${workflow.slug}`);
  await page.getByLabel("items").fill("[2, 9]");
  await page.getByRole("button", { name: "실행", exact: true }).click();
  const webForm = page.getByRole("region", { name: "사람 입력 노드가 입력을 기다립니다" });
  await expect(webForm).toBeVisible();
  await webForm.getByRole("textbox", { name: "답" }).fill("웹앱에서 확인");
  await webForm.getByRole("button", { name: "보내기" }).click();
  const webResult = page.getByRole("region", { name: "실행 결과" });
  await expect(webResult.getByText("웹앱에서 확인")).toBeVisible();
  await expect(webForm).toHaveCount(0);
});
