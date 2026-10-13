import { describe, expect, it } from "vitest";
import i18n from "@/i18n";
import { makeLoginSchema, makePasswordChangeSchema, makeSetupSchema } from "./schemas";

const t = i18n.getFixedT("ko", "common");

function messages(result: {
  success: boolean;
  error?: { issues: { path: PropertyKey[]; message: string }[] };
}) {
  return Object.fromEntries(
    (result.error?.issues ?? []).map((issue) => [issue.path.join("."), issue.message]),
  );
}

describe("로그인 스키마", () => {
  const schema = makeLoginSchema(t);

  it("서버와 같은 느슨한 이메일 규칙을 쓴다", () => {
    expect(schema.safeParse({ email: "me@local", password: "x" }).success).toBe(true);
    expect(schema.safeParse({ email: "  me@example.com  ", password: "x" }).data?.email).toBe(
      "me@example.com",
    );
    expect(messages(schema.safeParse({ email: "me", password: "" }))).toEqual({
      email: "이메일 형식이 아닙니다",
      password: "필수 항목입니다",
    });
  });
});

describe("초기 설정 스키마", () => {
  const schema = makeSetupSchema(t, 12);
  const valid = {
    email: "me@example.com",
    name: "",
    workspace_name: "",
    password: "correct-horse-1",
    password_confirm: "correct-horse-1",
  };

  it("서버가 알려 준 최소 길이로 비밀번호를 검사한다", () => {
    expect(schema.safeParse(valid).success).toBe(true);
    expect(
      messages(schema.safeParse({ ...valid, password: "short", password_confirm: "short" })),
    ).toEqual({
      password: "12자 이상이어야 합니다",
    });
  });

  it("비밀번호 확인이 다르면 확인 칸에 오류를 단다", () => {
    expect(messages(schema.safeParse({ ...valid, password_confirm: "correct-horse-2" }))).toEqual({
      password_confirm: "비밀번호가 서로 다릅니다",
    });
  });
});

describe("비밀번호 변경 스키마", () => {
  const schema = makePasswordChangeSchema(t, 12);

  it("현재 비밀번호와 새 비밀번호 확인을 검사한다", () => {
    expect(
      messages(
        schema.safeParse({
          current_password: "",
          new_password: "a".repeat(12),
          new_password_confirm: "b",
        }),
      ),
    ).toEqual({
      current_password: "필수 항목입니다",
      new_password_confirm: "비밀번호가 서로 다릅니다",
    });
  });
});
