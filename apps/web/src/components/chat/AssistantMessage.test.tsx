// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AssistantMessage } from "./AssistantMessage";

afterEach(cleanup);

function codeOf(markdown: string) {
  const { container } = render(<AssistantMessage text={markdown} streaming={false} />);
  return container.querySelector("pre code");
}

describe("AssistantMessage 코드 강조", () => {
  it("넣어 둔 언어와 그 별칭은 칠한다", () => {
    for (const block of [
      "```powershell\nGet-ChildItem -Path . | Select-Object Name\n```",
      "```ps1\n$items = Get-ChildItem\n```",
      "```py\nprint('한글')\n```",
      '```json\n{"a": 1}\n```',
    ]) {
      const code = codeOf(block);
      expect(code?.classList.contains("hljs")).toBe(true);
      expect(code?.querySelector("[class^='hljs-']")).not.toBeNull();
      cleanup();
    }
  });

  it("넣지 않은 언어와 언어 표시가 없는 블록은 글자 그대로 둔다", () => {
    for (const block of ["```rust\nfn main() {}\n```", "```\nplain text\n```"]) {
      const code = codeOf(block);
      expect(code?.classList.contains("hljs")).toBe(false);
      expect(code?.querySelector("span")).toBeNull();
      cleanup();
    }
  });

  it("스트리밍 중에는 칠하지 않는다", () => {
    const { container } = render(<AssistantMessage text={"```python\nx = 1\n```"} streaming />);
    expect(container.querySelector("pre code span")).toBeNull();
  });
});

// 설계 11장 S12: 모델 출력은 신뢰하지 않는다.
describe("AssistantMessage 원시 HTML과 링크", () => {
  it("모델이 쓴 HTML은 요소가 되지 않고 글자로 남는다", () => {
    const script = "<script>window.__xss = 1</script>";
    const img = '<img src="x" onerror="window.__xss = 2">';
    for (const streaming of [false, true]) {
      const { container } = render(
        <AssistantMessage text={`${script}\n\n앞 ${img} 뒤`} streaming={streaming} />,
      );
      expect(container.querySelector("script, img, [onerror]")).toBeNull();
      expect(container.textContent).toContain(script);
      expect(container.textContent).toContain(`앞 ${img} 뒤`);
      cleanup();
    }
  });

  it("링크와 마크다운 이미지는 새 창 링크로 열고 rel에 noopener를 붙인다", () => {
    const { container, getByRole } = render(
      <AssistantMessage
        text={"[문서](https://example.com/doc) ![그림](https://example.com/a.png?q=1)"}
        streaming={false}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    for (const [name, href] of [
      ["문서", "https://example.com/doc"],
      ["그림", "https://example.com/a.png?q=1"],
    ]) {
      const link = getByRole("link", { name });
      expect(link.getAttribute("href")).toBe(href);
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")?.split(" ")).toContain("noopener");
    }
  });
});
