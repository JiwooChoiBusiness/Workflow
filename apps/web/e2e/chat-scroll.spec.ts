import { expect, test } from "@playwright/test";

import { createAgent, preview, runStatus, send, setupFakeServer } from "./support.js";

setupFakeServer();

test("빌더 채팅 패널의 스크롤이 바깥 문서로 새지 않는다", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 600 });
  const agentId = await createAgent(page, "scrollbot");
  await page.goto(`/agents/${agentId}`);
  await expect(preview(page)).toBeVisible();
  for (let i = 0; i < 6; i++) {
    await send(page, `파일: 줄 ${i} ${"긴 내용 ".repeat(40)}`);
    await expect(runStatus(page)).toContainText(/완료|실패/, { timeout: 30_000 });
  }
  const measured = await page.evaluate(() => {
    const doc = document.scrollingElement as HTMLElement;
    const main = document.querySelector("main") as HTMLElement;
    const aside = document.querySelector("aside") as HTMLElement;
    const chat = Array.from(aside.querySelectorAll("*")).find((node) => {
      const el = node as HTMLElement;
      const style = getComputedStyle(el);
      return (
        (style.overflowY === "auto" || style.overflowY === "scroll") &&
        el.scrollHeight > el.clientHeight
      );
    }) as HTMLElement | undefined;
    return {
      docOverflow: doc.scrollHeight - doc.clientHeight,
      mainOverflow: main.scrollHeight - main.clientHeight,
      chatOverflow: chat ? chat.scrollHeight - chat.clientHeight : -1,
      asideHeight: aside.offsetHeight,
      mainHeight: main.clientHeight,
    };
  });
  expect(measured.chatOverflow).toBeGreaterThan(0);
  expect(measured.docOverflow).toBe(0);
  expect(measured.mainOverflow).toBe(0);

  const box = (await preview(page).boundingBox()) as {
    x: number;
    y: number;
    width: number;
    height: number;
  };
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 5000);
  await page.waitForTimeout(400);
  const after = await page.evaluate(() => ({
    doc: (document.scrollingElement as HTMLElement).scrollTop,
    main: (document.querySelector("main") as HTMLElement).scrollTop,
  }));
  expect(after.doc).toBe(0);
  expect(after.main).toBe(0);
});
