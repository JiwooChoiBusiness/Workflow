// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SqlRuntimeStatus } from "@/api/types";
import { RUNTIME_POLL_MS } from "@/features/sql/api";
import "@/features/sql/i18n";
import { RuntimeStatus } from "./RuntimeStatus";

const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: { GET: getMock, POST: postMock },
}));

const installing: SqlRuntimeStatus = {
  state: "installing",
  packages: ["duckdb==2.0.0", "pytz==2026.6"],
  extensions: ["excel", "encodings"],
  packages_ready: false,
  extensions_ready: false,
  bytes: null,
  installed_at: null,
  error: null,
};
// 서버 SqlRuntime._run_install이 SQL_RUNTIME_INSTALL_TIMEOUT_SEC를 넘겼을 때 남기는 실패.
const TIMEOUT_MESSAGE = "600초 안에 끝나지 않아 설치를 멈췄다. 인터넷 연결을 확인하고 다시 받는다";
const timedOut: SqlRuntimeStatus = {
  ...installing,
  state: "failed",
  error: { code: "sql_runtime_install_timeout", message: TIMEOUT_MESSAGE },
};
const ready: SqlRuntimeStatus = {
  ...installing,
  state: "ready",
  packages_ready: true,
  extensions_ready: true,
  bytes: 265_000_000,
  installed_at: "2026-11-04T12:00:00Z",
};

function answer(data: SqlRuntimeStatus) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  getMock.mockReset();
  postMock.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RuntimeStatus", () => {
  it("설치가 시간 상한으로 실패하면 폴링을 멈추고 오류 문장과 다시 받기를 보이며, 다시 받으면 이어서 읽는다", async () => {
    getMock
      .mockImplementationOnce(() => answer(installing))
      .mockImplementationOnce(() => answer(timedOut))
      .mockImplementation(() => answer(ready));
    postMock.mockImplementation(() => answer(installing));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <RuntimeStatus />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/받는 중입니다/)).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(RUNTIME_POLL_MS + 100));
    expect(await screen.findByText(TIMEOUT_MESSAGE)).toBeTruthy();
    expect(screen.getByText("런타임을 받지 못했습니다.")).toBeTruthy();
    expect(getMock).toHaveBeenCalledTimes(2);

    // failed에서는 더 읽지 않는다.
    await act(() => vi.advanceTimersByTimeAsync(RUNTIME_POLL_MS * 5));
    expect(getMock).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "다시 받기" }));
    expect(await screen.findByText(/받는 중입니다/)).toBeTruthy();
    expect(postMock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(RUNTIME_POLL_MS + 100));
    expect(await screen.findByRole("status", { name: "DuckDB 런타임" })).toBeTruthy();
    expect(screen.getByText(/준비됨/)).toBeTruthy();
  });
});
