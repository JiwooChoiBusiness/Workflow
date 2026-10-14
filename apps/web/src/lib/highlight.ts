import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { createLowlight } from "lowlight";

// 채팅에 자주 나오는 언어만 넣는다. rehype-highlight는 lowlight의 common(37개 언어)을 늘 묶어
// 채팅 묶음이 커진다. 별칭(sh, ps1, py, ts, js, yml, md, html 등)은 각 언어 정의에 들어 있다.
const lowlight = createLowlight({
  bash,
  css,
  javascript,
  json,
  markdown,
  powershell,
  python,
  sql,
  typescript,
  xml,
  yaml,
});

type Root = ReturnType<typeof lowlight.highlight>;
type Content = Root["children"][number];
type Element = Extract<Content, { type: "element" }>;

function languageOf(node: Element): string | null {
  const classes = node.properties.className;
  if (!Array.isArray(classes)) return null;
  let name: string | null = null;
  for (const item of classes) {
    const value = String(item);
    if (value === "no-highlight" || value === "nohighlight") return null;
    if (name === null && value.startsWith("language-")) name = value.slice("language-".length);
    else if (name === null && value.startsWith("lang-")) name = value.slice("lang-".length);
  }
  return name;
}

function textOf(node: Content): string {
  if (node.type === "text") return node.value;
  return node.type === "element" ? node.children.map(textOf).join("") : "";
}

function highlightCode(node: Element): void {
  const language = languageOf(node);
  if (language === null || !lowlight.registered(language)) return;
  const classes = Array.isArray(node.properties.className) ? node.properties.className : [];
  node.properties.className = ["hljs", ...classes.filter((name) => name !== "hljs")];
  const result = lowlight.highlight(language, node.children.map(textOf).join(""), {
    prefix: "hljs-",
  });
  if (result.children.length > 0) node.children = result.children as Element["children"];
}

function visit(parent: Root | Element): void {
  for (const child of parent.children) {
    if (child.type !== "element") continue;
    if (child.tagName === "code" && parent.type === "element" && parent.tagName === "pre") {
      highlightCode(child);
    } else {
      visit(child);
    }
  }
}

// rehype-highlight({detect: false})처럼 ```언어 로 표시한 코드 블록만 칠한다. 모르는 언어는 그대로 둔다.
export function rehypeHighlight() {
  return (tree: Root) => visit(tree);
}
