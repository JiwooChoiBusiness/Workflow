import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { knowledgeErrorMessage, type MessageOptions } from "./messages";

// 지식 화면의 오류를 문장으로. 업로드 한도처럼 문장에 들어갈 값은 부르는 쪽이 넘긴다.
export function useKnowledgeError(options?: MessageOptions) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const mb = options?.uploadMaxMb;
  const files = options?.uploadMaxFiles;
  return useCallback(
    (error: unknown) =>
      knowledgeErrorMessage(error, t, tc, { uploadMaxMb: mb, uploadMaxFiles: files }),
    [t, tc, mb, files],
  );
}
