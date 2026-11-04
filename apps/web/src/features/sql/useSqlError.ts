import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import "./i18n";
import { sqlErrorMessage, type SqlErrorOptions } from "./validation";

// 데이터 소스 화면의 오류를 문장으로. 파일 이름처럼 문장에 들어갈 값은 부르는 쪽이 넘긴다.
export function useSqlError(options?: SqlErrorOptions) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const fileName = options?.fileName;
  return useCallback(
    (error: unknown) => sqlErrorMessage(error, t, tc, { fileName }),
    [t, tc, fileName],
  );
}
