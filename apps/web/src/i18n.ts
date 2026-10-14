import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import agents from "@/locales/ko/agents.json";
import auth from "@/locales/ko/auth.json";
import chat from "@/locales/ko/chat.json";
import common from "@/locales/ko/common.json";
import logs from "@/locales/ko/logs.json";
import settings from "@/locales/ko/settings.json";
import skills from "@/locales/ko/skills.json";
import webapp from "@/locales/ko/webapp.json";

// 1단계는 한국어만 둔다(설계 7.1). 번역 파일은 묶음에 바로 넣어 초기화가 동기로 끝난다.
export const resources = {
  ko: { common, auth, agents, skills, settings, chat, logs, webapp },
} as const;

void i18n.use(initReactI18next).init({
  lng: "ko",
  fallbackLng: "ko",
  defaultNS: "common",
  ns: Object.keys(resources.ko),
  resources,
  initAsync: false,
  interpolation: { escapeValue: false },
  returnNull: false,
});

export default i18n;
