// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { useForm, type UseFormReturn } from "react-hook-form";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type {
  AgentOut,
  AgentVersionOut,
  AgentWarning,
  KnowledgeBaseOut,
  KnowledgeDefaults,
  SqlSourceSummary,
} from "@/api/types";
import { agentToForm, formToDraftUpdate, type AgentForm } from "@/features/agents/schemas";
import { knowledgeKeys } from "@/features/knowledge/api";
import { DEFAULT_RETRIEVAL } from "@/features/knowledge/retrievalSettings";
import { sqlKeys } from "@/features/sql/api";
import { KnowledgeTab } from "./KnowledgeTab";

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});
afterEach(cleanup);

const agent = {
  id: "a1",
  slug: "helper",
  name: "도우미",
  icon: null,
  description: "",
  webapp_enabled: false,
  draft_version_id: "v0",
  published_version_id: null,
  published_version_number: null,
  published_at: null,
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-03T00:00:00Z",
} as AgentOut;

const draft = {
  id: "v0",
  agent_id: "a1",
  version_number: null,
  status: "draft",
  instructions: "",
  model: { model_id: null, params: { max_output_tokens: 16000 } },
  tools: [],
  limits: {
    max_steps: 30,
    max_run_seconds: 1800,
    max_total_tokens: 2_000_000,
    tool_timeout_sec: 120,
    max_child_calls: 20,
  },
  sandbox: { memory_limit_mb: null, max_processes: null },
  approval_mode: "all_shell",
  env_vars: [],
  skill_version_ids: [],
  file_ids: [],
  knowledge: { kb_ids: [], retrieval_model: null },
  snapshot_id: null,
  published_at: null,
  change_note: null,
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-03T00:00:00Z",
} as unknown as AgentVersionOut;

function kb(id: string, name: string, tier: "free" | "paid", topK = 4): KnowledgeBaseOut {
  return {
    id,
    slug: id,
    name,
    icon: null,
    description: "",
    doc_form: "text_model",
    indexing_technique: "high_quality",
    embedding: null,
    process_rule: {},
    retrieval_model: { ...DEFAULT_RETRIEVAL, top_k: topK },
    search_while_indexing: true,
    built_in_fields_enabled: false,
    data_policy: { key_tier: tier, acknowledged_at: "2026-11-03T00:00:00Z" },
    document_count: 3,
    segment_count: 10,
    indexing_document_count: 0,
    embedded_tokens_estimated: 0,
    created_at: "2026-11-03T00:00:00Z",
    updated_at: "2026-11-03T00:00:00Z",
  };
}

function sqlSource(
  id: string,
  name: string,
  tier: "free" | "paid",
  state: SqlSourceSummary["state"],
): SqlSourceSummary {
  return {
    id,
    slug: id,
    name,
    description: "",
    kind: "sqlite",
    dialect: "sqlite",
    file_count: 1,
    table_count: 2,
    state,
    approval: "none",
    key_tier: tier,
    updated_at: "2026-11-04T00:00:00Z",
  };
}

let current: UseFormReturn<AgentForm> | null = null;

function Harness({
  warnings,
  onForm,
}: {
  warnings: AgentWarning[];
  onForm: (form: UseFormReturn<AgentForm>) => void;
}) {
  const form = useForm<AgentForm>({ defaultValues: agentToForm(agent, draft) });
  useEffect(() => {
    onForm(form);
  }, [form, onForm]);
  return <KnowledgeTab form={form} warnings={warnings} />;
}

function show(warnings: AgentWarning[] = []) {
  // 받아 둔 데이터만 쓰고 서버에 다시 묻지 않는다.
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(knowledgeKeys.list, [
    kb("kb-a", "사내 규정", "free", 6),
    kb("kb-b", "고객 계약", "paid"),
  ]);
  client.setQueryData(sqlKeys.list, [
    sqlSource("src-a", "쇼핑몰 DB", "free", "ready"),
    sqlSource("src-b", "매출 장부", "paid", "not_prepared"),
  ]);
  client.setQueryData(knowledgeKeys.defaults, {
    top_k_max: 20,
    paid_key: true,
    retrieval_model: DEFAULT_RETRIEVAL,
  } as unknown as KnowledgeDefaults);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness warnings={warnings} onForm={(form) => (current = form)} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("에이전트 빌더 지식 탭", () => {
  it("지식 베이스를 붙이면 search_knowledge가 붙는다고 알리고 초안에 kb_ids를 보낸다", async () => {
    const user = show();
    expect(screen.queryByText("search_knowledge 도구가 붙습니다")).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /사내 규정/ }));
    expect(screen.getByText("search_knowledge 도구가 붙습니다")).toBeTruthy();
    // 하나면 지식 베이스 설정을 이어받는다.
    expect(screen.getByText(/지식 베이스 설정\(하이브리드 · 상위 6\)으로 찾습니다/)).toBeTruthy();
    const body = formToDraftUpdate(current!.getValues(), draft.updated_at);
    expect(body.knowledge).toEqual({ kb_ids: ["kb-a"], retrieval_model: null });
  });

  it("둘 이상이면 검색 설정을 정해야 해서 첫 지식 베이스 설정에서 시작한다", async () => {
    const user = show();
    await user.click(screen.getByRole("checkbox", { name: /사내 규정/ }));
    await user.click(screen.getByRole("checkbox", { name: /고객 계약/ }));
    const inherit = screen.getByRole("switch", { name: "지식 베이스 설정 쓰기" });
    expect(inherit.getAttribute("aria-checked")).toBe("false");
    expect(inherit.hasAttribute("disabled")).toBe(true);
    const body = formToDraftUpdate(current!.getValues(), draft.updated_at);
    expect(body.knowledge?.kb_ids).toEqual(["kb-a", "kb-b"]);
    expect(body.knowledge?.retrieval_model).toMatchObject({ top_k: 6 });
  });

  it("저장 때 서버가 돌려준 검색 설정 칸 오류를 그 칸에 보인다", async () => {
    const user = show();
    await user.click(screen.getByRole("checkbox", { name: /사내 규정/ }));
    await user.click(screen.getByRole("checkbox", { name: /고객 계약/ }));
    act(() =>
      current!.setError("knowledge.retrieval.top_k", {
        type: "server",
        message: "top_k는 8 이하여야 한다",
      }),
    );
    expect(screen.getByText("top_k는 8 이하여야 한다")).toBeTruthy();
  });

  it("유료 키 전용 지식 베이스에는 배지를 달고 고르면 유료 키로 돈다고 알린다", async () => {
    const user = show();
    const paidRow = screen.getByRole("checkbox", { name: /고객 계약/ }).closest("li")!;
    expect(within(paidRow).getByText("유료 키 전용")).toBeTruthy();
    await user.click(screen.getByRole("checkbox", { name: /고객 계약/ }));
    expect(screen.getByText("유료 키 전용 지식 베이스")).toBeTruthy();
    expect(
      screen.getByText(/고객 계약을\(를\) 붙이면 이 에이전트의 런은 모두 유료 키로/),
    ).toBeTruthy();
  });

  it("저장 경고를 보인다(지운 지식 베이스, 하위 에이전트 때문인 유료 키)", () => {
    show([
      {
        code: "knowledge_base_not_found",
        message: "지식 베이스를 찾을 수 없다",
        agent_id: null,
        kb_id: "kb-gone",
      },
      {
        code: "paid_key_required",
        message: "하위 에이전트 계약 담당이 유료 키 전용 지식 베이스를 쓴다",
        agent_id: "a2",
        kb_id: null,
      },
    ]);
    const region = screen.getByRole("region", { name: "지식 베이스·데이터 소스 경고" });
    expect(within(region).getByText("지식 베이스를 찾을 수 없다")).toBeTruthy();
    expect(
      within(region).getByText(
        "하위 에이전트(또는 그 아래)가 유료 키 전용 지식 베이스나 데이터 소스를 씁니다.",
      ),
    ).toBeTruthy();
  });
});

describe("에이전트 빌더 지식 탭의 데이터 소스", () => {
  it("데이터 소스를 붙이면 SQL 도구가 붙는다고 알리고 초안에 sql_sources를 보낸다", async () => {
    const user = show();
    expect(screen.queryByText("describe_sql_source·query_sql 도구가 붙습니다")).toBeNull();
    expect(formToDraftUpdate(current!.getValues(), draft.updated_at).sql_sources).toEqual([]);
    await user.click(screen.getByRole("checkbox", { name: /쇼핑몰 DB/ }));
    expect(screen.getByText("describe_sql_source·query_sql 도구가 붙습니다")).toBeTruthy();
    expect(screen.getByText("1/10개 고름")).toBeTruthy();
    expect(formToDraftUpdate(current!.getValues(), draft.updated_at).sql_sources).toEqual([
      "src-a",
    ]);
  });

  it("유료 키 전용 소스에는 배지를 달고, 준비 전 소스를 고르면 도구가 실패한다고 알린다", async () => {
    const user = show();
    const paidRow = screen.getByRole("checkbox", { name: /매출 장부/ }).closest("li")!;
    expect(within(paidRow).getByText("유료 키 전용")).toBeTruthy();
    expect(within(paidRow).getByText("SQLite · 표 2 · 준비 전")).toBeTruthy();
    await user.click(screen.getByRole("checkbox", { name: /매출 장부/ }));
    expect(screen.getByText("유료 키 전용 데이터 소스")).toBeTruthy();
    expect(
      screen.getByText(/매출 장부을\(를\) 붙이면 이 에이전트의 런은 모두 유료 키로/),
    ).toBeTruthy();
    expect(screen.getByText(/준비되지 않은 데이터 소스가 있습니다/)).toBeTruthy();
  });

  it("지운 데이터 소스는 따로 보이고 뺄 수 있으며, 저장 경고는 그 줄에 붙는다", async () => {
    const user = show([
      {
        code: "sql_source_not_found",
        message: "데이터 소스를 찾을 수 없다. 지웠거나 다른 PC의 id다",
        agent_id: null,
        kb_id: null,
        source_id: "src-gone",
      },
    ]);
    act(() => current!.setValue("sql_sources", ["src-gone"], { shouldDirty: true }));
    expect(screen.getByText("찾을 수 없는 데이터 소스")).toBeTruthy();
    expect(
      screen.getAllByText("데이터 소스를 찾을 수 없다. 지웠거나 다른 PC의 id다").length,
    ).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "데이터 소스 src-gone 빼기" }));
    expect(current!.getValues("sql_sources")).toEqual([]);
  });
});
