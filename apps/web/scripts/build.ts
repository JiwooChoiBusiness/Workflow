// 화면 빌드. tsc -b로 타입을 확인하고 vite build를 dist.next에 한 뒤, 성공하면 dist와 바꿔치기한다.
//   node scripts/build.ts   (pnpm build가 부른다)
// vite build는 시작하면서 출력 폴더를 비운다. dist에 바로 빌드하다 실패하면 떠 있는 서버가 내줄 화면이
// 사라지므로 dist.next에 빌드하고, 실패하면 dist는 그대로 둔 채 dist.next만 지운다.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, rename, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const dist = join(root, "dist");
const next = join(root, "dist.next");
const old = join(root, "dist.old");

// 메모리 여유가 적은 PC에서 vite build(rolldown)가 "memory allocation of N bytes failed"로 죽었고 V8 힙
// 상한을 주면 끝까지 돌았다. 이미 있는 NODE_OPTIONS 뒤에 붙여 이 값이 이기게 한다.
const nodeOptions = [process.env.NODE_OPTIONS, "--max-old-space-size=1024"]
  .filter(Boolean)
  .join(" ");

// Windows는 node_modules/.bin의 .cmd 래퍼를 shell 없이 띄울 수 없어 패키지의 bin 스크립트를 이 node로 돌린다.
async function binScript(pkg: string, name: string): Promise<string> {
  const manifestPath = fileURLToPath(import.meta.resolve(`${pkg}/package.json`));
  const manifest = JSON.parse(await readFile(manifestPath, "utf8")) as {
    bin: string | Record<string, string>;
  };
  const bin = typeof manifest.bin === "string" ? manifest.bin : manifest.bin[name];
  return join(dirname(manifestPath), bin);
}

function run(script: string, args: string[]): number {
  const result = spawnSync(process.execPath, [script, ...args], {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env, NODE_OPTIONS: nodeOptions },
  });
  if (result.error) console.error(result.error.message);
  return result.status ?? 1;
}

function remove(path: string): Promise<void> {
  return rm(path, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}

// 백신·검색 인덱서나 떠 있는 서버가 안의 파일을 잠깐 열고 있으면 Windows가 폴더 이름 바꾸기를 거부한다.
async function move(from: string, to: string): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await rename(from, to);
      return;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code ?? "";
      if (attempt >= 20 || !["EPERM", "EACCES", "EBUSY"].includes(code)) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}

async function swap(): Promise<void> {
  await remove(old);
  const hadDist = existsSync(dist);
  if (hadDist) await move(dist, old);
  try {
    await move(next, dist);
  } catch (error) {
    if (hadDist) await move(old, dist);
    throw error;
  }
  try {
    await remove(old);
  } catch (error) {
    // 새 화면은 이미 dist에 있다. 남은 dist.old는 다음 빌드가 지운다.
    console.warn(`dist.old를 지우지 못했다: ${String(error)}`);
  }
}

async function main(): Promise<number> {
  // 앞선 빌드가 도중에 끊겨 남긴 폴더
  await remove(next);
  let code = run(await binScript("typescript", "tsc"), ["-b"]);
  if (code === 0) {
    code = run(await binScript("vite", "vite"), ["build", "--outDir", "dist.next"]);
    if (code === 0 && !existsSync(join(next, "index.html"))) code = 1;
  }
  if (code === 0) {
    try {
      await swap();
      return 0;
    } catch (error) {
      console.error(`dist.next를 dist로 옮기지 못했다: ${String(error)}`);
      code = 1;
    }
  }
  await remove(next);
  console.error("빌드가 실패해 dist는 그대로 둔다.");
  return code;
}

process.exitCode = await main();
