import { describe, expect, it } from "vitest";
import type { WorkspaceOut } from "@/api/types";
import i18n from "@/i18n";
import { formToWorkspacePatch, makeWorkspaceSettingsSchema, workspaceToForm } from "./schemas";

const t = i18n.getFixedT("ko", "common");

const workspace: WorkspaceOut = {
  id: "w1",
  name: "기본 워크스페이스",
  slug: "default",
  settings: {
    approval_mode_default: "all_shell",
    run_event_retention_days: 90,
    model_prices: { "gemini-3.8-flash": { input_per_mtok: 0.3, output_per_mtok: 2.5 } },
  },
  created_at: "2026-10-13T00:00:00Z",
  updated_at: "2026-10-13T00:00:00Z",
};

describe("워크스페이스 설정 스키마", () => {
  const schema = makeWorkspaceSettingsSchema(t);

  it("default_limits가 비어 있으면 서버 기본 한도로 채운다", () => {
    const form = workspaceToForm(workspace);
    expect(form.default_limits.max_steps).toBe(30);
    expect(form.model_prices).toEqual([
      {
        model: "gemini-3.8-flash",
        input_per_mtok: 0.3,
        output_per_mtok: 2.5,
        cached_input_per_mtok: null,
      },
    ]);
    expect(schema.safeParse(form).success).toBe(true);
  });

  it("가격 행을 모델별 사전으로 바꿔 보낸다", () => {
    const patch = formToWorkspacePatch(workspaceToForm(workspace));
    expect(patch.settings?.model_prices).toEqual({
      "gemini-3.8-flash": {
        input_per_mtok: 0.3,
        output_per_mtok: 2.5,
        cached_input_per_mtok: null,
      },
    });
  });

  it("같은 모델 가격이 두 번 있거나 단가가 음수면 막는다", () => {
    const form = workspaceToForm(workspace);
    const result = schema.safeParse({
      ...form,
      model_prices: [
        ...form.model_prices,
        {
          model: "gemini-3.8-flash",
          input_per_mtok: -1,
          output_per_mtok: 1,
          cached_input_per_mtok: null,
        },
      ],
    });
    expect(result.error?.issues.map((issue) => [issue.path.join("."), issue.message])).toEqual([
      ["model_prices.1.input_per_mtok", "0 이상이어야 합니다"],
      ["model_prices.1.model", "같은 이름이 이미 있습니다"],
    ]);
  });
});
