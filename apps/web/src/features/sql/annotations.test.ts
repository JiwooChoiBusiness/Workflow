import { describe, expect, it } from "vitest";
import {
  annotationsUpdate,
  hiddenChanged,
  sameDraft,
  setColumn,
  setTable,
  type TableNotes,
} from "./annotations";

const saved: TableNotes = {
  customers: {
    description: "고객",
    hidden: false,
    columns: { email: { description: "", hidden: true } },
  },
  customer_emails: { description: "", hidden: true, columns: {} },
};

describe("스키마 주석 편집", () => {
  it("열·표 숨김을 바꾸면 다시 준비해야 하는 변화로 본다", () => {
    expect(hiddenChanged(saved, setColumn(saved, "customers", "region", { hidden: true }))).toBe(
      true,
    );
    expect(hiddenChanged(saved, setColumn(saved, "customers", "email", { hidden: false }))).toBe(
      true,
    );
    expect(hiddenChanged(saved, setTable(saved, "orders", { hidden: true }))).toBe(true);
  });

  it("설명만 바꾸거나 숨김을 켰다 끄면 다시 준비할 일이 없다", () => {
    const described = setColumn(saved, "customers", "name", { description: "이름" });
    expect(hiddenChanged(saved, described)).toBe(false);
    const toggled = setTable(setTable(saved, "orders", { hidden: true }), "orders", {
      hidden: false,
    });
    expect(hiddenChanged(saved, toggled)).toBe(false);
  });

  it("숨김 비교는 서버처럼 대소문자를 가리지 않는다", () => {
    const renamed: TableNotes = {
      Customers: {
        description: "",
        hidden: false,
        columns: { EMAIL: { description: "", hidden: true } },
      },
      CUSTOMER_EMAILS: { description: "", hidden: true, columns: {} },
    };
    expect(hiddenChanged(saved, renamed)).toBe(false);
  });

  it("빈 주석은 빼고 설명의 앞뒤 공백을 덜어 보낸다", () => {
    const draft = {
      tables: setColumn(setTable(saved, "orders", { description: "  주문  " }), "orders", "id", {
        description: "",
        hidden: false,
      }),
      glossary: "매출 = amount 합계",
    };
    expect(annotationsUpdate(draft)).toEqual({
      tables: {
        customers: {
          description: "고객",
          hidden: false,
          columns: { email: { description: "", hidden: true } },
        },
        customer_emails: { description: "", hidden: true, columns: {} },
        orders: { description: "주문", hidden: false, columns: {} },
      },
      glossary: "매출 = amount 합계",
      prepare: true,
    });
  });

  it("고친 것이 없으면 키 순서·빈 주석과 관계없이 같은 초안이다", () => {
    const base = { tables: saved, glossary: "" };
    const reordered = {
      tables: {
        customer_emails: saved.customer_emails!,
        orders: { description: "", hidden: false, columns: {} },
        customers: saved.customers!,
      },
      glossary: "",
    };
    expect(sameDraft(base, reordered)).toBe(true);
    expect(sameDraft(base, { ...base, glossary: "용어" })).toBe(false);
  });
});
