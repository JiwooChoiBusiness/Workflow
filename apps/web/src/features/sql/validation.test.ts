import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/http";
import i18n from "@/i18n";
import "./i18n";
// 서버 소스를 경로로 읽어 코드 목록을 대조한다. validate.py·runner.py를 옮기거나 이름을 바꾸면 여기도 고친다.
// jsdom 환경은 apps/web 밖 파일을 ?raw로 읽지 못하므로 이 파일은 node 환경으로 둔다.
import validateSource from "../../../../api/app/knowledge/sql/validate.py?raw";
import runnerSource from "../../../../api/app/knowledge/sql/runner.py?raw";
import {
  failureKind,
  failureText,
  isSqlCode,
  REJECT_CODES,
  RUN_CODES,
  SOURCE_CODES,
  splitNodeError,
  sqlCodeText,
  sqlErrorMessage,
  TABLE_NAME,
} from "./validation";

const t = i18n.getFixedT("ko", "sql");
const tc = i18n.getFixedT("ko", "common");

// 서버 소스에서 SqlRejected("<코드>"…)·SqlRunFailed("<코드>"…)로 내는 코드를 모은다. 여러 줄에 걸친 호출도 잡는다.
function codesIn(source: string, cls: string): string[] {
  const pattern = new RegExp(`${cls}\\(\\s*"([a-z_]+)"`, "g");
  return [...new Set([...source.matchAll(pattern)].map((match) => match[1]!))].sort();
}

describe("SQL 오류 코드 → 문장", () => {
  it("검사가 거부하는 코드를 모두 다룬다(서버 validate.py와 같은 목록)", () => {
    expect([...REJECT_CODES].sort()).toEqual(codesIn(validateSource, "SqlRejected"));
  });

  it("실행 실패 코드를 모두 다룬다(서버 runner.py, 런타임 설치 실패는 데이터 소스 코드로)", () => {
    const server = codesIn(runnerSource, "SqlRunFailed").filter(
      (code) => code !== "sql_runtime_install_failed",
    );
    const mapped = /_FAILURE_CODES = \{([^}]*)\}/.exec(runnerSource)?.[1] ?? "";
    const fromMap = [...mapped.matchAll(/:\s*"([a-z_]+)"/g)].map((match) => match[1]!);
    expect([...RUN_CODES].sort()).toEqual([...new Set([...server, ...fromMap])].sort());
    expect(SOURCE_CODES).toContain("sql_runtime_install_failed");
  });

  it.each([...REJECT_CODES, ...RUN_CODES, ...SOURCE_CODES])(
    "%s는 키 이름이 아니라 사용자 문장이다",
    (code) => {
      const text = sqlCodeText(code, t);
      expect(text).toBeTruthy();
      expect(text).not.toContain(code);
      expect(text).not.toContain("reject.");
    },
  );

  it("새 코드 cte_name_conflict·column_unresolved를 알아듣는 말로 바꾼다", () => {
    expect(sqlCodeText("column_unresolved", t)).toBe(
      "숨긴 열이 있는 표에서는 열을 표 이름이나 별칭으로 분명히 써 주세요(USING·NATURAL 대신 ON).",
    );
    expect(sqlCodeText("cte_name_conflict", t)).toMatch(/WITH로 붙인 이름이/);
  });

  it("바꾼 문장 아래에 서버 문장(어느 표·열인지)을 남기고, 모르는 코드는 서버 문장을 그대로 쓴다", () => {
    expect(
      failureText(
        { code: "column_hidden", message: "열 customers.email은(는) 숨긴 열이라 쓸 수 없다" },
        t,
      ),
    ).toEqual({
      title: "숨긴 열은 쓸 수 없습니다. 그 열을 빼거나 데이터 소스에서 숨김을 푸세요.",
      detail: "열 customers.email은(는) 숨긴 열이라 쓸 수 없다",
    });
    expect(failureText({ code: "brand_new", message: "새 이유" }, t)).toEqual({
      title: "새 이유",
      detail: null,
    });
    expect(isSqlCode("brand_new")).toBe(false);
  });

  it("sql_query 노드의 error 출력(코드: 문장)을 나눈다", () => {
    expect(splitNodeError("")).toBeNull();
    expect(splitNodeError("not_select: SELECT 문만 실행한다(DELETE 문은 쓸 수 없다)")).toEqual({
      code: "not_select",
      message: "SELECT 문만 실행한다(DELETE 문은 쓸 수 없다)",
    });
    expect(splitNodeError("코드 없는 글")).toEqual({ code: "", message: "코드 없는 글" });
  });

  it("노드 실패는 검사 거부·승인 거절·데이터 소스 상태·실행 실패로 나눠 배지를 단다", () => {
    for (const code of REJECT_CODES) expect(failureKind(code)).toBe("validate");
    expect(failureKind("sql_rejected")).toBe("rejected");
    for (const code of SOURCE_CODES.filter((item) => item !== "sql_rejected")) {
      expect(failureKind(code)).toBe("source");
    }
    expect(failureKind("sql_runtime_missing")).toBe("source");
    for (const code of ["sql_error", "sql_timeout", "sql_memory_limit", "sql_failed", "", "x"]) {
      expect(failureKind(code)).toBe("execute");
    }
    expect([t("stage.rejected"), t("stage.source"), t("stage.execute")]).toEqual([
      "거절",
      "실행하지 않음",
      "실행 실패",
    ]);
  });
});

describe("데이터 소스 화면 오류", () => {
  it("413은 서버 한도(MB)를, 422·409는 예시 저장 사유를 문장으로", () => {
    expect(
      sqlErrorMessage(
        new ApiError(413, "file_too_large", "x", { max_bytes: 10 * 1024 * 1024 }),
        t,
        tc,
      ),
    ).toBe("파일이 너무 큽니다. 파일 하나는 10MB까지입니다.");
    expect(sqlErrorMessage(new ApiError(415, "unsupported_file_type", "x"), t, tc)).toMatch(
      /넣을 수 없는 파일 형식/,
    );
    expect(sqlErrorMessage(new ApiError(422, "kb_not_qa", "x"), t, tc)).toBe(
      "예시는 Q&A 모드 지식 베이스에만 넣을 수 있습니다.",
    );
    expect(
      sqlErrorMessage(
        new ApiError(409, "duplicate_document", "x", {
          duplicates: [{ document_name: "쇼핑몰 SQL 예시: 지역별 고객 수" }],
        }),
        t,
        tc,
      ),
    ).toBe("같은 질문과 SQL이 이미 들어 있습니다: 쇼핑몰 SQL 예시: 지역별 고객 수");
  });

  it("준비 실패는 파일 이름을, 409 상태 오류는 코드 문장을 쓴다", () => {
    const prepare = new ApiError(422, "sql_prepare_failed", "인코딩을 알 수 없다", {
      file_id: "f1",
    });
    expect(sqlErrorMessage(prepare, t, tc, { fileName: () => "매출.csv" })).toBe(
      "매출.csv을(를) 읽지 못했습니다. 인코딩·머리글·시트 옵션을 확인하세요. 인코딩을 알 수 없다",
    );
    expect(sqlErrorMessage(new ApiError(409, "sql_runtime_missing", "x"), t, tc)).toMatch(
      /DuckDB 런타임이 없습니다/,
    );
    const taken = new ApiError(422, "validation_error", "x", [
      { loc: ["body", "files"], msg: "겹친다", type: "table_name_taken" },
    ]);
    expect(sqlErrorMessage(taken, t, tc)).toBe("표 이름이 겹칩니다. 파일마다 다른 이름을 쓰세요.");
  });

  it("표 이름 규칙은 서버 TableName과 같다", () => {
    expect(TABLE_NAME.test("sales_2026")).toBe(true);
    expect(TABLE_NAME.test("Sales")).toBe(false);
    expect(TABLE_NAME.test("2026_sales")).toBe(false);
    expect(TABLE_NAME.test(`a${"b".repeat(62)}`)).toBe(true);
    expect(TABLE_NAME.test(`a${"b".repeat(63)}`)).toBe(false);
  });
});
