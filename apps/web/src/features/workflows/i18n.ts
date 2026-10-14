import i18n from "@/i18n";
import strings from "@/locales/ko/workflows.json";

// 워크플로우 화면 문구는 그 화면 묶음과 함께 받는다. 첫 화면(index) 묶음에 넣지 않는다.
if (!i18n.hasResourceBundle("ko", "workflows")) {
  i18n.addResourceBundle("ko", "workflows", strings, true, true);
}
