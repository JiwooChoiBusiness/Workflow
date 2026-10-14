import "i18next";
import type dev from "@/locales/ko/dev.json";
import type workflows from "@/locales/ko/workflows.json";
import type { resources } from "./i18n";

// 번역 키를 타입으로 검사한다. dev 네임스페이스는 개발용 화면이, workflows는 워크플로우 화면이 실행 중에
// 추가한다(첫 화면 묶음에 넣지 않으려고).
declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "common";
    resources: (typeof resources)["ko"] & { dev: typeof dev; workflows: typeof workflows };
  }
}
