import i18n from "@/i18n";
import strings from "@/locales/ko/sql.json";

// 데이터 소스 화면·SQL 노드 폼·승인 카드의 문구는 그 화면 묶음과 함께 받는다. 첫 화면 묶음에 넣지 않는다.
if (!i18n.hasResourceBundle("ko", "sql")) {
  i18n.addResourceBundle("ko", "sql", strings, true, true);
}
