// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import nodeTypeList from "@/test/fixtures/contract/node-types.json";
import {
  commonFields,
  conditionProblems,
  emptyFilterForm,
  filterRequest,
  fromFilterForm,
  toFilterForm,
  type FilterField,
  type MetadataFilterForm,
} from "@/features/knowledge/metadataFilter";
import { MetadataFilterBuilder, type VariableSlot } from "./MetadataFilterBuilder";

// 서버가 주는 연산자 표(GET /node-types의 kb_options.operators)
const operators = nodeTypeList.kb_options.operators as Record<string, string[]>;

const fields: FilterField[] = [
  { name: "category", type: "string" },
  { name: "year", type: "number" },
  { name: "published", type: "time" },
];

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});
afterEach(cleanup);

describe("메타데이터 필터 값", () => {
  it("여러 지식 베이스에서는 같은 이름·타입으로 모두에 있는 필드만 고를 수 있다", () => {
    const first: FilterField[] = fields;
    const second: FilterField[] = [
      { name: "category", type: "string" },
      { name: "year", type: "string" },
    ];
    expect(commonFields([first, second])).toEqual([{ name: "category", type: "string" }]);
    expect(commonFields([first])).toEqual(fields);
    expect(commonFields([])).toEqual([]);
  });

  it("저장된 config를 폼으로 읽고 되돌리면 값의 타입이 그대로다(별칭은 서버 이름으로)", () => {
    const config = {
      mode: "manual",
      logical_operator: "or",
      conditions: [
        { name: "category", comparison_operator: "starts with", value: "규정" },
        { name: "year", comparison_operator: ">=", value: 2024 },
        { name: "year", comparison_operator: "in", value: [2023, 2024] },
        { name: "category", comparison_operator: "is", value: { var: "start.kind" } },
        { name: "category", comparison_operator: "is empty", value: null },
      ],
      model: { model_id: null },
    };
    const form = toFilterForm(config);
    expect(form.conditions.map((item) => item.comparison_operator)).toEqual([
      "start with",
      "≥",
      "in",
      "is",
      "empty",
    ]);
    expect(fromFilterForm(form).conditions).toEqual([
      { name: "category", comparison_operator: "start with", value: "규정" },
      { name: "year", comparison_operator: "≥", value: 2024 },
      { name: "year", comparison_operator: "in", value: [2023, 2024] },
      { name: "category", comparison_operator: "is", value: { var: "start.kind" } },
      { name: "category", comparison_operator: "empty", value: null },
    ]);
    // 끄거나 자동으로 바꿔도 조건은 남는다(서버는 manual일 때만 조건을 본다).
    expect(fromFilterForm({ ...form, mode: "disabled" }).conditions).toHaveLength(5);
    expect(fromFilterForm({ ...form, mode: "automatic" }).conditions).toHaveLength(5);
  });

  it("저장된 조건의 타입은 필드 목록에서 정한다(값 모양 짐작은 목록을 읽기 전에만)", () => {
    const form = toFilterForm({
      mode: "manual",
      conditions: [
        {
          name: "published",
          comparison_operator: "before",
          value: "2026-01-01T00:00:00+09:00",
        },
        { name: "year", comparison_operator: "≥", value: { var: "start.min" } },
      ],
    });
    // 값만 보면 둘 다 글로 짐작한다.
    expect(form.conditions.map((item) => item.type)).toEqual(["string", "string"]);
    // 필드 목록이 있으면 time·number 연산자로 보아 거짓 오류가 없다. 읽기 전에는 연산자를 보지 않는다.
    expect(conditionProblems(form, fields, operators)).toEqual({});
    expect(conditionProblems(form, null, operators)).toEqual({});

    // 변수 값을 상수로 바꾸면 필드 타입(number)으로 보낸다.
    const edited: MetadataFilterForm = {
      ...form,
      conditions: [
        { ...form.conditions[0]!, value_text: "1767225600" },
        { ...form.conditions[1]!, source: "const", value_text: "2024", var: "" },
      ],
    };
    expect(fromFilterForm(edited, fields).conditions.map((item) => item.value)).toEqual([
      1767225600, 2024,
    ]);
    expect(filterRequest(edited, fields)?.conditions?.map((item) => item.value)).toEqual([
      1767225600, 2024,
    ]);
    // 숫자 필드라 숫자가 아닌 값은 문제다(짐작한 글 타입이었다면 지나갔다).
    const wrong: MetadataFilterForm = {
      ...edited,
      conditions: [edited.conditions[0]!, { ...edited.conditions[1]!, value_text: "올해" }],
    };
    expect(conditionProblems(wrong, fields, operators)).toEqual({ 1: { value: "number" } });
  });

  it("검색 시험 요청은 수동 조건이 있을 때만 상수로 만든다", () => {
    expect(filterRequest(emptyFilterForm())).toBeNull();
    const form: MetadataFilterForm = {
      ...emptyFilterForm(),
      mode: "manual",
      conditions: [
        {
          name: "published",
          type: "time",
          comparison_operator: "after",
          source: "const",
          value_text: "1767225600",
          var: "",
        },
      ],
    };
    expect(filterRequest(form)).toEqual({
      mode: "manual",
      logical_operator: "and",
      conditions: [{ name: "published", comparison_operator: "after", value: 1767225600 }],
      model: { model_id: null },
    });
  });

  it("칸별 문제: 없는 필드, 타입에 없는 연산자, 숫자가 아닌 값, 빈 변수", () => {
    const form: MetadataFilterForm = {
      ...emptyFilterForm(),
      mode: "manual",
      conditions: [
        {
          name: "gone",
          type: "string",
          comparison_operator: "is",
          source: "const",
          value_text: "x",
          var: "",
        },
        {
          name: "year",
          type: "number",
          comparison_operator: "contains",
          source: "const",
          value_text: "올해",
          var: "",
        },
        {
          name: "category",
          type: "string",
          comparison_operator: "is",
          source: "var",
          value_text: "",
          var: "",
        },
      ],
    };
    expect(conditionProblems(form, fields, operators)).toEqual({
      0: { name: "unknownField" },
      1: { comparison_operator: "operator", value: "number" },
      2: { value: "var" },
    });
  });
});

function Harness({
  initial,
  onValue,
  withVariables = false,
}: {
  initial: MetadataFilterForm;
  onValue: (value: MetadataFilterForm) => void;
  withVariables?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <MetadataFilterBuilder
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue(next);
      }}
      fields={fields}
      operators={operators}
      renderVariable={
        withVariables
          ? (slot: VariableSlot) => <span data-testid="variable-slot">{slot.label}</span>
          : undefined
      }
    />
  );
}

describe("MetadataFilterBuilder", () => {
  it("조건을 더하면 첫 필드와 그 타입의 첫 연산자로 시작하고 값을 넣는다", async () => {
    const user = userEvent.setup();
    let latest = emptyFilterForm();
    render(
      <Harness
        initial={{ ...emptyFilterForm(), mode: "manual" }}
        onValue={(value) => (latest = value)}
      />,
    );
    await user.click(screen.getByRole("button", { name: "조건 추가" }));
    expect(latest.conditions).toEqual([
      {
        name: "category",
        type: "string",
        comparison_operator: operators.string![0],
        source: "const",
        value_text: "",
        var: "",
      },
    ]);
    const row = screen.getByRole("listitem", { name: "조건 1" });
    expect(within(row).getByRole("alert").textContent).toBe("값을 넣으세요.");
    await user.type(within(row).getByRole("textbox", { name: "조건 1 값" }), "규정");
    expect(latest.conditions[0]?.value_text).toBe("규정");
    expect(within(row).queryByRole("alert")).toBeNull();
    // 상수만 쓰는 곳(검색 시험)에는 값 방식 칸이 없다.
    expect(within(row).queryByRole("combobox", { name: "조건 1 값 방식" })).toBeNull();
  });

  it("숫자 필드에 숫자가 아닌 값을 넣으면 줄 아래에 알린다", () => {
    render(
      <Harness
        initial={{
          ...emptyFilterForm(),
          mode: "manual",
          conditions: [
            {
              name: "year",
              type: "number",
              comparison_operator: "=",
              source: "const",
              value_text: "올해",
              var: "",
            },
          ],
        }}
        onValue={() => undefined}
      />,
    );
    const row = screen.getByRole("listitem", { name: "조건 1" });
    expect(within(row).getByRole("alert").textContent).toBe("숫자를 넣으세요.");
  });

  it("노드 폼은 값으로 변수를 고를 수 있다", () => {
    render(
      <Harness
        initial={{
          ...emptyFilterForm(),
          mode: "manual",
          conditions: [
            {
              name: "category",
              type: "string",
              comparison_operator: "is",
              source: "var",
              value_text: "",
              var: "start.kind",
            },
          ],
        }}
        onValue={() => undefined}
        withVariables
      />,
    );
    const row = screen.getByRole("listitem", { name: "조건 1" });
    expect(within(row).getByRole("combobox", { name: "조건 1 값 방식" })).toBeTruthy();
    expect(within(row).getByTestId("variable-slot").textContent).toBe("조건 1 값");
  });

  it("저장된 time 조건은 필드 목록의 타입으로 연산자를 고르고 거짓 오류가 없다", () => {
    render(
      <Harness
        initial={toFilterForm({
          mode: "manual",
          conditions: [{ name: "published", comparison_operator: "after", value: "2026-01-01" }],
        })}
        onValue={() => undefined}
      />,
    );
    const row = screen.getByRole("listitem", { name: "조건 1" });
    expect(within(row).getByRole("combobox", { name: "조건 1 연산자" }).textContent).toBe("이후");
    expect(within(row).queryByRole("alert")).toBeNull();
    expect(
      within(row).getByRole("textbox", { name: "조건 1 값" }).getAttribute("placeholder"),
    ).toBe("2026-11-03T09:00:00+09:00 또는 Unix 초");
  });

  it("필드를 읽는 중이면 그렇다고 보이고, 못 읽었으면 다시 시도할 수 있다", async () => {
    const user = userEvent.setup();
    const manual = { ...emptyFilterForm(), mode: "manual" as const };
    const { rerender } = render(
      <MetadataFilterBuilder
        value={manual}
        onChange={() => undefined}
        fields={null}
        operators={operators}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("메타데이터 필드를 읽는 중입니다.");
    expect(screen.getByRole("button", { name: "조건 추가" }).hasAttribute("disabled")).toBe(true);

    let retried = 0;
    rerender(
      <MetadataFilterBuilder
        value={manual}
        onChange={() => undefined}
        fields={null}
        fieldsError={new Error("네트워크 오류")}
        onRetryFields={() => (retried += 1)}
        operators={operators}
      />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("메타데이터 필드를 불러오지 못했습니다.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(retried).toBe(1);
  });

  it("끈 모드에서는 조건 줄을 보이지 않는다", () => {
    render(<Harness initial={emptyFilterForm()} onValue={() => undefined} />);
    expect(screen.queryByRole("button", { name: "조건 추가" })).toBeNull();
  });
});
