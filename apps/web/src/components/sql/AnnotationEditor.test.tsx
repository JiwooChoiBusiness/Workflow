// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SqlSourceOut } from "@/api/types";
import "@/features/sql/i18n";
import { AnnotationEditor } from "./AnnotationEditor";

const mutate = vi.fn();

vi.mock("@/features/sql/api", () => ({
  useUpdateAnnotations: () => ({ mutate, isPending: false }),
  useExportSchema: () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false }),
}));
vi.mock("@/features/knowledge/api", () => ({
  useKnowledgeBases: () => ({ data: [], isPending: false }),
}));

afterEach(cleanup);
beforeEach(() => mutate.mockReset());

function source(overrides: Partial<SqlSourceOut> = {}): SqlSourceOut {
  return {
    id: "s1",
    slug: "shop",
    name: "쇼핑몰",
    description: "",
    kind: "sqlite",
    dialect: "sqlite",
    files: [],
    schema: {
      tables: [
        {
          name: "customers",
          kind: "table",
          columns: [
            { name: "id", type: "INTEGER", samples: [1, 2] },
            { name: "email", type: "TEXT", samples: [] },
            { name: "region", type: "TEXT", samples: ["서울", "부산"] },
          ],
          row_count: 3,
        },
      ],
      captured_at: "2026-11-04T00:00:00Z",
      source_hash: "h1",
      warnings: [],
    },
    annotations: {
      tables: {
        customers: {
          description: "",
          hidden: false,
          columns: { email: { description: "", hidden: true } },
        },
      },
    },
    glossary: "",
    limits: { max_rows: 200, timeout_sec: 30, memory_mb: 512, llm_rows: 50, llm_bytes: 65536 },
    approval: "none",
    data_policy: { key_tier: "free", acknowledged_at: "2026-11-04T00:00:00Z" },
    materialized: null,
    status: { state: "ready", runtime_required: false, message: "" },
    created_at: "2026-11-04T00:00:00Z",
    updated_at: "2026-11-04T00:00:00Z",
    ...overrides,
  };
}

function show(value = source()) {
  render(
    <MemoryRouter>
      <AnnotationEditor source={value} />
    </MemoryRouter>,
  );
  return userEvent.setup();
}

describe("스키마 설명·숨김 편집", () => {
  it("표·열·타입·샘플을 보이고 숨긴 열의 스위치는 켜져 있다", () => {
    show();
    const table = screen.getByRole("region", { name: "표 customers" });
    expect(within(table).getByText("서울, 부산")).toBeTruthy();
    expect(
      screen.getByRole("switch", { name: "customers.email 숨기기" }).getAttribute("aria-checked"),
    ).toBe("true");
    expect(screen.queryByRole("button", { name: "스키마 설명 저장" })).toBeNull();
  });

  it("숨김을 바꾸면 다시 준비해야 한다고 알리고, 저장은 prepare=true로 보낸다", async () => {
    const user = show();
    await user.click(screen.getByRole("switch", { name: "customers.region 숨기기" }));
    expect(
      screen.getByText(/저장하면 숨김을 뺀 질의 파일\(또는 사본\)을 다시 만들고/),
    ).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "스키마 설명 저장" }));
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0]![0]).toEqual({
      tables: {
        customers: {
          description: "",
          hidden: false,
          columns: {
            email: { description: "", hidden: true },
            region: { description: "", hidden: true },
          },
        },
      },
      glossary: "",
      prepare: true,
    });
  });

  it("설명만 바꾸면 다시 준비 안내 없이 저장한다", async () => {
    const user = show();
    await user.type(screen.getByRole("textbox", { name: "customers 설명" }), "고객 목록");
    expect(
      screen.queryByText(/저장하면 숨김을 뺀 질의 파일\(또는 사본\)을 다시 만들고/),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "스키마 설명 저장" }));
    expect(mutate.mock.calls[0]![0].tables.customers.description).toBe("고객 목록");
  });

  it("숨김을 바꾼 뒤 다시 준비하지 못해 stale이면 서버가 알린 이유를 보인다", () => {
    show(
      source({
        status: {
          state: "stale",
          runtime_required: false,
          message: "숨김을 바꿨지만 다시 준비하지 못했다: 원본 파일이 없다",
        },
      }),
    );
    expect(screen.getByText("숨김을 바꿨지만 다시 준비하지 못했다: 원본 파일이 없다")).toBeTruthy();
    expect(screen.getByText(/다시 준비하기 전까지는 질의할 수 없습니다/)).toBeTruthy();
  });
});
