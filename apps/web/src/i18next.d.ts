import "i18next";
import type dev from "@/locales/ko/dev.json";
import type { resources } from "./i18n";

// 번역 키를 타입으로 검사한다. dev 네임스페이스는 개발용 화면이 실행 중에 추가한다.
declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "common";
    resources: (typeof resources)["ko"] & { dev: typeof dev };
  }
}
