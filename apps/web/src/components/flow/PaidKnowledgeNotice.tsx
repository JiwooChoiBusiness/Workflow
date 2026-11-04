import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { useKbOptions } from "@/features/workflows/api";
import { useFlow } from "@/features/workflows/flowStoreContext";

/*
 * 유료 키 전용 지식 베이스를 쓰는 워크플로우(3단계 14.4). 서버 검증의 paid_key_required 경고가 있으면 런의
 * 모든 모델 호출이 유료 키로 간다는 것과, 유료 키가 없으면 실행·발행할 수 없다는 것을 알린다.
 */
export function PaidKnowledgeNotice() {
  const { t } = useTranslation("workflows");
  const paid = useFlow((state) => state.issues.some((issue) => issue.code === "paid_key_required"));
  const options = useKbOptions().data;
  if (!paid) return null;
  return (
    <Alert tone="warn" title={t("settings.paidTitle")}>
      {options && !options.paid_key_configured ? t("settings.paidMissing") : t("settings.paidHint")}
    </Alert>
  );
}
