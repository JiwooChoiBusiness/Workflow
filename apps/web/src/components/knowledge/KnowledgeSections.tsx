import { useTranslation } from "react-i18next";
import { NavLink } from "react-router";
import "@/features/knowledge/i18n";
import { cn } from "@/lib/cn";

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "-mb-px inline-flex h-9 items-center border-b-2 px-3 text-sm whitespace-nowrap transition-colors",
    isActive
      ? "border-accent font-medium text-ink"
      : "border-transparent text-ink-muted hover:text-ink",
  );

// 지식 화면의 상단 탭 "지식 베이스 / 데이터 소스"(설계 12.3). 두 목록은 주소가 따로라 링크로 옮겨 다닌다.
export function KnowledgeSections() {
  const { t } = useTranslation("knowledge");
  return (
    <nav aria-label={t("sections.label")} className="mb-5 flex gap-1 border-b border-line">
      <NavLink to="/knowledge" end className={linkClass}>
        {t("sections.knowledge")}
      </NavLink>
      <NavLink to="/knowledge/sql-sources" className={linkClass}>
        {t("sections.sqlSources")}
      </NavLink>
    </nav>
  );
}
