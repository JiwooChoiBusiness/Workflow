import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";

// 지식 색인 파일(index.db)을 쓸 수 없을 때(설계 5.2). 목록·KB 화면·만들기·올리기가 같은 문구를 쓴다.
export function IndexUnavailableNotice({
  reason,
  className,
}: {
  reason?: string | null;
  className?: string;
}) {
  const { t } = useTranslation("knowledge");
  return (
    <Alert tone="warn" title={t("index.unavailable")} className={className}>
      <span className="block">{t("index.unavailableHint")}</span>
      {reason ? <span className="block text-xs">{t("index.reason", { reason })}</span> : null}
    </Alert>
  );
}
