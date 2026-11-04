// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError } from "@/api/http";
import { PublishPanelView, type PublishSource } from "./PublishPanel";

afterEach(cleanup);

function refusingSource(error: unknown): PublishSource {
  return {
    versions: { data: [], isPending: false, isError: false, error: null, refetch: () => undefined },
    publish: { isPending: false, mutate: (_note, options) => options.onError(error) },
    restore: { isPending: false, mutate: () => undefined },
  };
}

describe("발행 거부 알림", () => {
  it("초안을 고치기 시작하면 내리고, 고친 뒤 저장해도 다시 나오지 않는다", async () => {
    const user = userEvent.setup();
    const source = refusingSource(
      new ApiError(422, "draft_invalid", "초안에 문제가 있어 발행할 수 없다", [
        {
          loc: ["knowledge", "retrieval_model"],
          msg: "지식 베이스를 여럿 고르면 검색 설정을 정한다",
          type: "retrieval_settings_required",
        },
      ]),
    );
    const save = async () => true;
    const { rerender } = render(
      <PublishPanelView source={source} dirty={false} beforePublish={save} />,
    );
    await user.click(screen.getByRole("button", { name: "발행" }));
    expect(screen.getByText("발행하지 못했습니다")).toBeTruthy();
    expect(screen.getByText("지식 베이스를 여럿 고르면 검색 설정을 정한다")).toBeTruthy();

    rerender(<PublishPanelView source={source} dirty beforePublish={save} />);
    expect(screen.queryByText("발행하지 못했습니다")).toBeNull();
    rerender(<PublishPanelView source={source} dirty={false} beforePublish={save} />);
    expect(screen.queryByText("발행하지 못했습니다")).toBeNull();
  });
});
