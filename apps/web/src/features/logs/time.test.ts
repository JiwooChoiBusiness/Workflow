import { describe, expect, it } from "vitest";
import { formatRelative } from "@/lib/format";
import { formatPast } from "./time";

const NOW = Date.UTC(2026, 9, 13, 12, 0, 0);

describe("지난 시각 표시", () => {
  it("시계보다 늦게 기록된 시각도 미래로 보이지 않는다", () => {
    const soon = new Date(NOW + 27_000).toISOString();

    expect(formatRelative(soon, NOW)).toBe("27초 후");
    expect(formatPast(soon, NOW)).toBe("지금");
    expect(formatPast(new Date(NOW - 120_000).toISOString(), NOW)).toBe("2분 전");
    expect(formatPast(null, NOW)).toBe("-");
  });
});
