import { readFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import {
  expect as baseExpect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { createAgent, preview, send, setupFakeServer } from "./support.ts";

/*
 * Text2SQL 화면(3단계 설계 16장 M28의 검증 순서). 가짜 어댑터 서버(8502)를 쓰고 실제 Gemini는 부르지 않는다.
 *
 * - SQLite 소스로 할 수 있는 단계는 모두 SQLite로 한다. SQLite는 샌드박스 파이썬의 sqlite3만 써서 DuckDB 런타임
 *   (약 250MB)을 받지 않는다. 시험용 .sqlite 파일은 node:sqlite로 그때그때 만든다. node:sqlite는 Node 22.13+·
 *   23.4+부터 플래그 없이 쓸 수 있다(README 기준 Node 24면 된다).
 * - Text2SQL 템플릿의 SQL 만들기 LLM은 가짜 어댑터 기본 응답("가짜 응답: <사용자 글>")을 그대로 쓴다. 메시지에
 *   ```sql 코드 블록을 넣으면 그 응답에서 sql_query가 같은 SQL을 꺼내므로 각본 서버가 따로 필요 없다.
 * - CSV 소스 시험만 DuckDB 런타임이 있어야 한다. 런타임을 한 번 받아 둔 DATA_DIR을 다시 쓰고, 없으면 건너뛴다.
 *   처음 한 번은 E2E_SQL_RUNTIME=install로 돌리면 "런타임 준비"를 눌러 받는다(인터넷 필요, 1분 안팎).
 *     SERVER_PORT=8502 DATA_DIR=C:\wf-scratch-sql LLM_FAKE=1 uv run platform serve   # 같은 폴더를 계속 쓴다
 *     E2E_SQL_RUNTIME=install corepack pnpm --dir apps/web e2e text2sql             # 처음 한 번
 *     corepack pnpm --dir apps/web e2e text2sql                                      # 다음부터는 받아 둔 런타임
 * - 에이전트가 query_sql을 부르는 시험(도구 카드의 결과 표·잘린 결과 CSV)은 각본 서버에서만 돈다. 각본은 서버
 *   전체가 순서대로 한 번만 쓰므로 그 서버에서는 모델을 부르는 다른 시험을 건너뛴다.
 *     ... LLM_FAKE=1 LLM_FAKE_SCRIPT=apps/web/e2e/fixtures/sql_agent_query.json uv run platform serve
 *     E2E_FAKE_SCRIPT=sql_agent_query corepack pnpm --dir apps/web e2e text2sql
 */

const expect = baseExpect.configure({ timeout: 15_000 });
const RUNTIME = process.env.E2E_SQL_RUNTIME ?? "";
const SCRIPT = process.env.E2E_FAKE_SCRIPT ?? "";
// sql_agent_query.json 각본이 부르는 데이터 소스. 같은 DATA_DIR에서 다시 돌면 있던 것을 쓴다.
const AGENT_SOURCE_SLUG = "e2e-sql-agent";
const AGENT_SOURCE_NAME = "e2e 에이전트 주문 DB";

setupFakeServer();

const FENCED =
  "```sql\nSELECT region, COUNT(*) AS n FROM customers GROUP BY region ORDER BY region\n```";

// customers(email은 개인정보라 숨길 열), orders, 숨긴 열을 읽는 뷰 하나.
function sqliteFile(path: string): string {
  const db = new DatabaseSync(path);
  db.exec(`
    create table customers(id integer primary key, name text, email text, region text);
    create table orders(id integer primary key, customer_id integer, amount real, ordered_at text);
    insert into customers values
      (1, '김민수', 'minsu@example.com', '서울'),
      (2, '이영희', 'yh@example.com', '부산'),
      (3, '박철수', 'cs@example.com', '서울');
    insert into orders values
      (1, 1, 12000, '2026-10-01'), (2, 1, 8000, '2026-10-03'),
      (3, 2, 30000, '2026-10-05'), (4, 3, 5000, '2026-10-07');
    create view customer_emails as select name, email from customers;
  `);
  db.close();
  return path;
}

async function knowledgeBase(
  request: APIRequestContext,
  name: string,
  docForm: "qa_model" | "text_model",
): Promise<string> {
  const created = await request.post("/api/console/knowledge", {
    data: {
      name,
      doc_form: docForm,
      indexing_technique: "economy",
      search_while_indexing: true,
      data_policy: { key_tier: "free", acknowledged: true },
    },
  });
  expect(created.status()).toBe(201);
  return (await created.json()).id as string;
}

// API로 SQLite 소스를 만들어 파일을 올리고 준비한다(화면 시험이 아닌 단계의 준비물).
async function preparedSqliteSource(
  page: Page,
  name: string,
  slug: string | null = null,
): Promise<{ id: string; slug: string }> {
  const created = await page.request.post("/api/console/sql-sources", {
    data: { name, slug, kind: "sqlite", data_policy: { key_tier: "free", acknowledged: true } },
  });
  expect(created.status()).toBe(201);
  const source = (await created.json()) as { id: string; slug: string };
  const file = sqliteFile(test.info().outputPath("shop.sqlite"));
  const uploaded = await page.request.post(`/api/console/sql-sources/${source.id}/files`, {
    headers: { "X-Requested-With": "fetch" },
    multipart: {
      files: {
        name: "shop.sqlite",
        mimeType: "application/octet-stream",
        buffer: await readFile(file),
      },
    },
  });
  expect(uploaded.ok()).toBeTruthy();
  const prepared = await page.request.post(`/api/console/sql-sources/${source.id}/prepare`, {
    headers: { "X-Requested-With": "fetch" },
  });
  expect(prepared.ok()).toBeTruthy();
  return source;
}

async function choose(page: Page, combobox: Locator, option: string | RegExp): Promise<void> {
  await combobox.click();
  await page.getByRole("option", { name: option, exact: typeof option === "string" }).click();
}

async function say(scope: Locator | Page, text: string): Promise<void> {
  const box = scope.getByRole("textbox", { name: "메시지" });
  await box.fill(text);
  await box.press("Enter");
}

async function runSql(page: Page, sql: string): Promise<void> {
  await page.getByRole("textbox", { name: "SQL", exact: true }).fill(sql);
  await page.getByRole("button", { name: "실행", exact: true }).click();
}

test("데이터 소스(SQLite): 만들기·올리기·준비, 스키마 설명과 숨김(다시 준비), SQL 시험의 거부 문장·결과 표·CSV, 예시 저장, 스키마 내보내기", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const stamp = Date.now();
  const name = `e2e 쇼핑몰 ${stamp}`;
  const qaKb = `e2e SQL 예시 ${stamp}`;
  const textKb = `e2e 스키마 ${stamp}`;
  const qaKbId = await knowledgeBase(page.request, qaKb, "qa_model");
  const textKbId = await knowledgeBase(page.request, textKb, "text_model");

  // 지식 화면 상단 탭으로 데이터 소스 목록에 간다.
  await page.goto("/knowledge");
  await page
    .getByRole("navigation", { name: "지식 화면" })
    .getByRole("link", { name: "데이터 소스" })
    .click();
  await expect(page).toHaveURL(/\/knowledge\/sql-sources$/);
  await expect(page.getByRole("heading", { name: "데이터 소스", level: 1 })).toBeVisible();

  // 만들기: 데이터 정책을 확인해야 만든다.
  await page.getByRole("button", { name: "새 데이터 소스" }).first().click();
  const dialog = page.getByRole("dialog", { name: "새 데이터 소스" });
  await dialog.getByLabel("이름").fill(name);
  await dialog.getByRole("radio", { name: /^SQLite/ }).click();
  await dialog.getByRole("radio", { name: /^실행 전 승인/ }).click();
  await expect(dialog.getByText(/결과 표의 일부가 채팅 모델로 갑니다/)).toBeVisible();
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(dialog.getByText("데이터 정책 안내를 확인해야 저장할 수 있습니다.")).toBeVisible();
  await dialog.getByRole("checkbox", { name: /이 데이터 소스에 넣을 파일이/ }).click();
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(page).toHaveURL(/\/knowledge\/sql-sources\/[0-9a-f-]{36}$/);
  const sourceId = new URL(page.url()).pathname.split("/").pop()!;
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  const prepare = page.getByRole("region", { name: "준비 상태" });
  await expect(prepare.getByText("파일 없음", { exact: true })).toBeVisible();

  // 올리기: SQLite 파일 하나. 올리면 준비 전이 된다.
  const file = sqliteFile(test.info().outputPath("shop.sqlite"));
  await page.getByTestId("sql-file-input").setInputFiles(file);
  await expect(page.getByText("shop.sqlite")).toBeVisible();
  await expect(prepare.getByText("준비 전", { exact: true })).toBeVisible();

  // 준비: 숨김이 없으면 원본을 그대로 읽는다.
  await prepare.getByRole("button", { name: "준비", exact: true }).click();
  await expect(prepare.getByText("준비됨", { exact: true })).toBeVisible();
  await expect(page.getByTestId("sql-prepare-facts")).toContainText("표 3개");
  await expect(page.getByTestId("sql-prepare-facts")).toContainText("원본을 그대로 읽습니다");

  // 스키마: 표 설명을 쓰고 email 열을 숨기면 다시 준비해야 한다고 알리고, 저장하면 숨김을 뺀 사본으로 다시 준비한다.
  await page.getByRole("tab", { name: "스키마" }).click();
  const customers = page.getByRole("region", { name: "표 customers" });
  await expect(customers.getByText("서울, 부산")).toBeVisible();
  await customers.getByRole("textbox", { name: "customers 설명" }).fill("고객 목록");
  await customers.getByRole("switch", { name: "customers.email 숨기기" }).click();
  await expect(
    page.getByText(/저장하면 숨김을 뺀 질의 파일\(또는 사본\)을 다시 만들고/),
  ).toBeVisible();
  await page.getByRole("textbox", { name: "용어집" }).fill("매출 = orders.amount 합계");
  await page.getByRole("button", { name: "스키마 설명 저장" }).click();
  await expect(page.getByText("저장하고 다시 준비했습니다.", { exact: true })).toBeVisible();
  await expect(page.getByTestId("sql-prepare-facts")).toContainText("숨김을 뺀 사본");
  // 숨긴 열을 읽는 뷰는 자동으로 숨긴다.
  await expect(
    prepare.getByText(/뷰 customer_emails이\(가\) 숨긴 표·열을 읽어 함께 숨겼습니다/),
  ).toBeVisible();
  const saved = await (await page.request.get(`/api/console/sql-sources/${sourceId}`)).json();
  expect(saved.annotations.tables.customers).toMatchObject({
    description: "고객 목록",
    columns: { email: { hidden: true } },
  });
  expect(saved.annotations.tables.customer_emails.hidden).toBe(true);
  expect(saved.glossary).toBe("매출 = orders.amount 합계");

  // SQL 시험: 거부·실패는 코드별 문장으로 보인다.
  await page.getByRole("tab", { name: "SQL 시험" }).click();
  const failure = page.getByTestId("sql-failure-text");
  await runSql(page, "SELECT email FROM customers");
  await expect(failure).toHaveText(
    "숨긴 열은 쓸 수 없습니다. 그 열을 빼거나 데이터 소스에서 숨김을 푸세요.",
  );
  await expect(page.getByText(/customers\.email은\(는\) 숨긴 열이라/)).toBeVisible();
  await runSql(page, "SELECT * FROM customers NATURAL JOIN orders");
  await expect(failure).toHaveText(
    "숨긴 열이 있는 표에서는 열을 표 이름이나 별칭으로 분명히 써 주세요(USING·NATURAL 대신 ON).",
  );
  await runSql(page, "DELETE FROM orders");
  await expect(failure).toContainText("SELECT 문만 실행합니다");
  await runSql(page, "WITH customers AS (SELECT 1 AS x) SELECT x FROM customers");
  await expect(failure).toContainText("WITH로 붙인 이름이");
  await runSql(page, "SELECT nope FROM orders");
  await expect(failure).toContainText("데이터베이스가 이 SQL을 실행하다 오류를 냈습니다");

  // 결과 표·실행한 SQL·CSV 내려받기
  const ranSql = "SELECT region, COUNT(*) AS n FROM customers GROUP BY region ORDER BY region";
  await runSql(page, ranSql);
  await expect(page.getByTestId("sql-result-summary")).toContainText("2행");
  const result = page.getByRole("table", { name: "결과 표" });
  await expect(result.getByRole("cell", { name: "서울" })).toBeVisible();
  await expect(page.getByRole("region", { name: "결과 표" }).getByText(/LIMIT 201/)).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "CSV 내려받기" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.csv$/);
  const csv = await readFile(await download.path(), "utf8");
  expect(
    csv
      .replace(/^\uFEFF/, "")
      .split("\r\n")
      .slice(0, 3),
  ).toEqual(["region,n", "부산,1", "서울,2"]);

  // 예시로 저장: 실행한 뒤 편집기를 고쳐도 결과를 낸 SQL이 들어간다. Q&A 지식 베이스에 들어가고, 같은 쌍은
  // 다시 넣지 않는다.
  await page.getByRole("textbox", { name: "SQL", exact: true }).fill("SELECT 1");
  for (const attempt of [1, 2]) {
    await page.getByRole("button", { name: "예시로 저장" }).click();
    const example = page.getByRole("dialog", { name: "예시로 저장" });
    await expect(example.getByRole("textbox", { name: "SQL" })).toHaveValue(ranSql);
    await example.getByLabel("질문").fill("지역별 고객 수");
    await choose(page, example.getByRole("combobox", { name: "Q&A 지식 베이스" }), qaKb);
    await example.getByRole("button", { name: "저장" }).click();
    if (attempt === 1) {
      await expect(page.getByText(`'${qaKb}'에 예시를 넣었습니다.`, { exact: true })).toBeVisible();
    } else {
      await expect(example.getByText(/같은 질문과 SQL이 이미 들어 있습니다/)).toBeVisible();
      await example.getByRole("button", { name: "취소" }).click();
    }
  }
  const documents = await (
    await page.request.get(`/api/console/knowledge/${qaKbId}/documents`)
  ).json();
  expect((documents.items as { name: string }[]).map((item) => item.name)).toEqual([
    `${name} SQL 예시: 지역별 고객 수`,
  ]);

  // 기록: 거부와 성공이 남는다.
  const logs = page.getByRole("table", { name: "기록" });
  await expect(logs.getByText("거부").first()).toBeVisible();
  await expect(logs.getByText("성공").first()).toBeVisible();

  // 스키마를 지식 베이스로 내보내기: 보이는 표(customers, orders)마다 문서 하나, 다시 하면 이전 문서를 바꾼다.
  await page.getByRole("tab", { name: "스키마" }).click();
  for (const attempt of [1, 2]) {
    await page.getByRole("button", { name: "지식 베이스로 내보내기" }).click();
    const exporter = page.getByRole("dialog", { name: "스키마를 지식 베이스로 내보내기" });
    await choose(page, exporter.getByRole("combobox", { name: "지식 베이스" }), textKb);
    await exporter.getByRole("button", { name: "내보내기" }).click();
    await expect(exporter.getByText("문서 2개를 넣었습니다.").first()).toBeVisible();
    await expect(exporter.getByRole("link", { name: `${name} · customers` })).toBeVisible();
    if (attempt === 2) {
      await expect(exporter.getByText("이전에 내보낸 문서 2개를 지웠습니다.")).toBeVisible();
    }
    await exporter.getByRole("button", { name: "닫기" }).last().click();
  }
  const exported = await (
    await page.request.get(`/api/console/knowledge/${textKbId}/documents`)
  ).json();
  expect(exported.total).toBe(2);

  // 설정 탭에서 고치던 칸은 위의 "다시 준비"로 데이터 소스가 바뀌어도 남고, 저장하면 저장된 값으로 채운다.
  await page.getByRole("tab", { name: "설정" }).click();
  const description = page.getByRole("textbox", { name: "설명", exact: true });
  await description.fill("e2e 설명 고치는 중");
  await prepare.getByRole("button", { name: "다시 준비" }).click();
  await expect(page.getByText("준비했습니다.", { exact: true })).toBeVisible();
  await expect(description).toHaveValue("e2e 설명 고치는 중");
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("설정을 저장했습니다.", { exact: true })).toBeVisible();
  await expect(description).toHaveValue("e2e 설명 고치는 중");
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();

  // 목록 카드에 상태와 승인 방식이 보인다.
  await page.goto("/knowledge/sql-sources");
  const card = page.getByRole("listitem").filter({ hasText: name });
  await expect(card.getByText("준비됨", { exact: true })).toBeVisible();
  await expect(card.getByText("실행 전 승인", { exact: true })).toBeVisible();
});

test("Text2SQL: 템플릿 챗플로우의 노드 폼, 미리보기의 SQL 승인 카드(고쳐서 거부·승인·거절), 웹앱, 에이전트 데이터 소스", async ({
  page,
}) => {
  test.skip(SCRIPT !== "", "각본 서버에서는 에이전트 query_sql 시험만 돈다");
  test.setTimeout(240_000);
  const stamp = Date.now();
  const sourceName = `e2e 주문 DB ${stamp}`;
  const source = await preparedSqliteSource(page, sourceName);

  // 템플릿 text_to_sql은 챗플로우에만 있고 데이터 소스를 고르면 세 SQL 노드에 넣는다.
  await page.goto("/workflows");
  await page.getByRole("button", { name: "새 워크플로우" }).first().click();
  const dialog = page.getByRole("dialog", { name: "새 워크플로우" });
  await dialog.getByLabel("이름").fill(`e2e Text2SQL ${stamp}`);
  await expect(dialog.getByRole("radio", { name: /표에 묻고 답하기/ })).toHaveCount(0);
  await dialog.getByRole("radio", { name: /^챗플로우/ }).click();
  await dialog.getByRole("radio", { name: /표에 묻고 답하기/ }).click();
  await choose(page, dialog.getByRole("combobox", { name: "데이터 소스" }), sourceName);
  await dialog.getByRole("button", { name: "만들기" }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]{36}$/);
  const workflowId = new URL(page.url()).pathname.split("/").pop()!;

  const canvas = page.getByRole("region", { name: "워크플로우 캔버스" });
  const panel = page.getByRole("complementary", { name: "워크플로우 설정" });
  await page.getByRole("button", { name: "화면에 맞추기" }).click();
  await expect(canvas.getByTestId("flow-node-schema")).toContainText(sourceName);
  await expect(canvas.getByTestId("flow-node-schema")).toContainText("표 전부");
  await expect(canvas.getByTestId("flow-node-run_sql")).toContainText("{{ write_sql.text }}");

  // SQL 스키마 폼: 데이터 소스와 보이는 표 목록(숨긴 것 없음), 데이터 정책 문구. 템플릿 그래프는 넓어서
  // 화면에 맞추면 카드가 작아지므로 주소의 ?node=로 노드를 고른다.
  await page.goto(`/workflows/${workflowId}?node=schema`);
  await expect(panel.getByRole("combobox", { name: "데이터 소스" })).toContainText(sourceName);
  await expect(panel.getByRole("checkbox", { name: "customers" })).toBeVisible();
  await expect(panel.getByText(/스키마 글\(표·열 이름과 설명, 샘플값, 용어집\)/)).toBeVisible();

  // SQL 실행 폼: 템플릿 SQL, 비운 한도는 데이터 소스 값을 따른다는 안내, 실행 전 승인을 항상으로.
  await page.goto(`/workflows/${workflowId}?node=run_sql`);
  await expect(panel.getByRole("textbox", { name: "실행할 SQL" })).toContainText(
    "{{ write_sql.text }}",
  );
  await expect(panel.getByText(/비우면 데이터 소스 값\(200\)을 따릅니다/)).toBeVisible();
  await choose(page, panel.getByRole("combobox", { name: "실행 전 승인" }), "항상 승인");
  await expect(canvas.getByTestId("flow-node-run_sql")).toContainText("실행 전 승인");
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: /저장 상태: 저장됨/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "검증 결과: 문제 없음" })).toBeVisible();
  const draft = await (await page.request.get(`/api/console/workflows/${workflowId}/draft`)).json();
  const nodes = draft.graph.nodes as { id: string; config: Record<string, unknown> }[];
  expect(nodes.find((item) => item.id === "run_sql")?.config).toMatchObject({
    source_id: source.id,
    approval: "always",
  });

  // 미리보기 1턴: 승인 카드에서 SQL을 쓰기 문장으로 고쳐 승인하면 다시 검사해 거부한다(KS11). 실패 가지가 고친
  // SQL을 승인 없이(데이터 소스 설정) 다시 실행해 답한다.
  await page.getByRole("banner").getByRole("button", { name: "미리보기", exact: true }).click();
  await say(panel, FENCED);
  const card = panel.getByRole("region", { name: "SQL 승인 요청" });
  await expect(card).toBeVisible();
  await expect(card.getByText(`데이터 소스 ${sourceName}`)).toBeVisible();
  await expect(card.getByRole("textbox", { name: "실행할 SQL" })).toContainText("GROUP BY");
  await card.getByRole("textbox", { name: "실행할 SQL" }).fill("DELETE FROM orders");
  await expect(card.getByText("고침")).toBeVisible();
  await card.getByRole("button", { name: "승인하고 실행" }).click();
  const status = panel.getByRole("status", { name: "워크플로우 런 상태" });
  await expect(status).toContainText("완료");
  await panel.getByRole("tab", { name: "실행 과정" }).click();
  const rejected = panel.getByTestId("node-run-run_sql");
  await expect(rejected).toHaveAttribute("data-status", "failed");
  await expect(rejected.getByTestId("sql-node-failure")).toContainText("SELECT 문만 실행합니다");
  await expect(rejected.getByText("검사에서 거부", { exact: true })).toBeVisible();
  await expect(rejected.getByText("DELETE FROM orders").first()).toBeVisible();
  const rerun = panel.getByTestId("node-run-rerun_sql");
  await expect(rerun).toHaveAttribute("data-status", "succeeded");
  await rerun.getByRole("button").first().click();
  await expect(rerun.getByRole("table", { name: "SQL 결과 표" }).getByText("서울")).toBeVisible();
  await expect(rerun.getByTestId("sql-node-rows")).toHaveText("2행");
  await panel.getByRole("tab", { name: "대화", exact: true }).click();

  // 2턴: 그대로 승인하면 실행하고 성공 가지로 답한다.
  await say(panel, FENCED);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "승인하고 실행" }).click();
  await expect(status).toContainText("완료");
  await panel.getByRole("tab", { name: "실행 과정" }).click();
  await expect(panel.getByTestId("node-run-run_sql")).toHaveAttribute("data-status", "succeeded");
  await expect(panel.getByTestId("node-run-answer")).toHaveAttribute("data-status", "succeeded");
  await panel.getByRole("tab", { name: "대화", exact: true }).click();

  // 3턴: 거절하면 sql_rejected로 실패하고 노드 카드에는 실행 실패가 아니라 "거절" 배지가 붙는다.
  await say(panel, FENCED);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "거절" }).click();
  await expect(status).toContainText("완료");
  await panel.getByRole("tab", { name: "실행 과정" }).click();
  const declined = panel.getByTestId("node-run-run_sql");
  await expect(declined.getByTestId("sql-node-failure")).toHaveText("SQL 실행을 거절했습니다.");
  await expect(declined.getByText("거절", { exact: true })).toBeVisible();
  await panel.getByRole("tab", { name: "대화", exact: true }).click();
  // 거절하면 고치기 가지로 가지 않고 템플릿이 정해 둔 문장으로 답한다.
  await expect(
    panel.getByText(
      "SQL 실행이 승인되지 않았거나 승인 대기 시간이 지나 이번 질문에는 답하지 않았습니다.",
    ),
  ).toBeVisible();

  // 발행하고 웹앱에서도 같은 승인 카드로 답한다.
  await panel.getByRole("tab", { name: "발행" }).click();
  await panel.getByRole("button", { name: "발행", exact: true }).click();
  await expect(page.getByText("v1을(를) 발행했습니다.", { exact: true })).toBeVisible();
  const enabled = await page.request.patch(`/api/console/workflows/${workflowId}`, {
    data: { webapp_enabled: true },
  });
  expect(enabled.ok()).toBeTruthy();
  const { slug } = await (await page.request.get(`/api/console/workflows/${workflowId}`)).json();
  await page.goto(`/flows/${slug}`);
  await say(page, FENCED);
  const webCard = page.getByRole("region", { name: "SQL 승인 요청" });
  await expect(webCard).toBeVisible();
  await webCard.getByRole("button", { name: "승인하고 실행" }).click();
  await expect(webCard).toHaveCount(0);
  await expect(page.getByText(/가짜 응답:/).first()).toBeVisible();

  // 에이전트 빌더 지식 탭에서 데이터 소스를 붙이면 도구 탭에 SQL 도구가 자동으로 보인다.
  const agentId = await createAgent(page, `e2e SQL 에이전트 ${stamp}`);
  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "지식" }).click();
  await page.getByRole("checkbox", { name: new RegExp(sourceName) }).click();
  await expect(page.getByText("describe_sql_source·query_sql 도구가 붙습니다")).toBeVisible();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "도구와 스킬" }).click();
  await expect(
    page.getByText(/데이터 소스 1개의 표 정의를 보고 SELECT를 실행합니다/),
  ).toBeVisible();
  const agentDraft = await (await page.request.get(`/api/console/agents/${agentId}/draft`)).json();
  expect(agentDraft.sql_sources).toEqual([source.id]);
});

test("데이터 소스(CSV): 런타임을 받아 둔 서버에서 표 이름·준비(질의 파일 크기)·숨김 열 다시 준비·질의", async ({
  page,
}) => {
  test.setTimeout(420_000);
  const runtime = await (await page.request.get("/api/console/sql-runtime")).json();
  test.skip(
    runtime.state !== "ready" && RUNTIME !== "install",
    "DuckDB 런타임이 없어 건너뛴다. E2E_SQL_RUNTIME=install로 한 번 받아 두면 같은 DATA_DIR에서 돈다",
  );
  const stamp = Date.now();
  const name = `e2e 매출 CSV ${stamp}`;
  const created = await page.request.post("/api/console/sql-sources", {
    data: { name, kind: "files", data_policy: { key_tier: "free", acknowledged: true } },
  });
  expect(created.status()).toBe(201);
  const sourceId = (await created.json()).id as string;
  await page.goto(`/knowledge/sql-sources/${sourceId}`);

  // 런타임: 없으면 준비 단추로 받고(202) 끝날 때까지 상태를 다시 읽는다.
  const runtimeStatus = page.getByRole("status", { name: "DuckDB 런타임" });
  if (runtime.state !== "ready") {
    await expect(page.getByText(/약 250MB를 내려받고 인터넷 연결이 필요합니다/)).toBeVisible();
    await page.getByRole("button", { name: "런타임 준비" }).click();
    await expect(page.getByText(/받는 중입니다/)).toBeVisible();
    await expect(runtimeStatus).toBeVisible({ timeout: 300_000 });
  }
  await expect(runtimeStatus).toContainText("준비됨");

  // 파일과 표 이름: 파일 이름에서 만든 이름을 바꾸면 다시 준비해야 한다.
  const csv = "region,amount,owner\n서울,12000,김민수\n부산,30000,이영희\n서울,8000,박철수\n";
  await page
    .getByTestId("sql-file-input")
    .setInputFiles({ name: "월별 매출.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  const table = page.getByRole("textbox", { name: "월별 매출.csv 표 이름" });
  await expect(table).toHaveValue("table");
  await table.fill("Sales");
  await expect(
    page.getByText(/영문 소문자로 시작하고 소문자·숫자·밑줄만 쓸 수 있습니다/),
  ).toBeVisible();
  await table.fill("sales");
  await page.getByRole("button", { name: "변경 저장" }).click();
  await expect(
    page.getByText("파일 설정을 저장했습니다. 다시 준비해야 질의할 수 있습니다.", { exact: true }),
  ).toBeVisible();

  const prepare = page.getByRole("region", { name: "준비 상태" });
  await prepare.getByRole("button", { name: "준비", exact: true }).click();
  await expect(prepare.getByText("준비됨", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("sql-prepare-facts")).toContainText(
    /질의 파일 [\d.,]+\s?(B|kB|MB)/,
  );

  // 숨김 열을 바꾸면 질의 파일을 다시 만든다.
  await page.getByRole("tab", { name: "스키마" }).click();
  await page.getByRole("switch", { name: "sales.owner 숨기기" }).click();
  await page.getByRole("button", { name: "스키마 설명 저장" }).click();
  await expect(page.getByText("저장하고 다시 준비했습니다.", { exact: true })).toBeVisible({
    timeout: 60_000,
  });

  await page.getByRole("tab", { name: "SQL 시험" }).click();
  await runSql(page, "SELECT owner FROM sales");
  await expect(page.getByTestId("sql-failure-text")).toContainText("숨긴 열은 쓸 수 없습니다");
  await runSql(
    page,
    "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region",
  );
  await expect(page.getByTestId("sql-result-summary")).toContainText("2행");
  await expect(page.getByRole("table", { name: "결과 표" }).getByText("20000")).toBeVisible();
});

test("에이전트: 미리보기의 query_sql 도구 카드에 행 수·잘림·실행한 SQL·결과 표와 잘린 결과 CSV가 보인다", async ({
  page,
}) => {
  test.skip(
    SCRIPT !== "sql_agent_query",
    "LLM_FAKE_SCRIPT=sql_agent_query.json으로 띄운 서버에서만 돈다",
  );
  test.setTimeout(120_000);
  const listed = await (await page.request.get("/api/console/sql-sources")).json();
  const existing = (listed.items as { id: string; slug: string }[]).find(
    (item) => item.slug === AGENT_SOURCE_SLUG,
  );
  const source =
    existing ?? (await preparedSqliteSource(page, AGENT_SOURCE_NAME, AGENT_SOURCE_SLUG));
  const agentId = await createAgent(page, `e2e SQL 도구 에이전트 ${Date.now()}`);

  await page.goto(`/agents/${agentId}`);
  await page.getByRole("tab", { name: "지식" }).click();
  await page.getByRole("checkbox", { name: new RegExp(AGENT_SOURCE_NAME) }).click();
  await page.getByRole("button", { name: "저장", exact: true }).click();
  await expect(page.getByText("초안을 저장했습니다.", { exact: true })).toBeVisible();
  const agentDraft = await (await page.request.get(`/api/console/agents/${agentId}/draft`)).json();
  expect(agentDraft.sql_sources).toEqual([source.id]);

  // 각본: query_sql(행 상한 1) → 답. 읽기 전용 도구라 승인 없이 돈다. 결과가 잘려 작업공간 sql/에 CSV가 남는다.
  await send(page, "지역별 고객 수를 알려 줘");
  const panel = preview(page);
  await expect(panel.getByText(/지역별 고객 수를 표로 정리했습니다/)).toBeVisible();
  await expect(panel.getByText("query_sql", { exact: true })).toBeVisible();
  await expect(panel.getByText("1행", { exact: true })).toBeVisible();
  await expect(panel.getByText("잘림 · 받은 행(행 상한까지)은 CSV")).toBeVisible();
  await expect(panel.getByLabel("실행한 SQL")).toContainText("LIMIT 2");
  const table = panel.getByRole("table", { name: "결과 표" });
  await expect(table.getByRole("cell", { name: "부산" })).toBeVisible();
  await expect(panel.getByRole("link", { name: /잘린 결과 CSV/ })).toHaveAttribute(
    "href",
    /download$/,
  );

  // 새로 고치면 기록에서 다시 그린다. 행 수·잘림·실행한 SQL은 결과 글에서 다시 읽는다.
  await page.reload();
  await expect(panel.getByText("query_sql", { exact: true })).toBeVisible();
  await expect(panel.getByText("1행", { exact: true })).toBeVisible();
  await expect(panel.getByText("잘림 · 받은 행(행 상한까지)은 CSV")).toBeVisible();
  await expect(panel.getByLabel("실행한 SQL")).toContainText("LIMIT 2");
  await expect(table.getByRole("cell", { name: "부산" })).toBeVisible();
  await expect(panel.getByRole("link", { name: /잘린 결과 CSV/ })).toBeVisible();
});
