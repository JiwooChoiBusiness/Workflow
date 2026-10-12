import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import App from "./App.tsx";

describe("App", () => {
  it("준비 중 안내를 보여준다", () => {
    expect(renderToString(<App />)).toContain("준비 중");
  });
});
