import { Database, Ellipsis, FolderOpen, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import type { SqlSourceSummary } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { relativeBase } from "@/features/knowledge/documentStatus";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { SqlStateBadge } from "./SqlStateBadge";

// 데이터 소스 목록의 카드: 이름·설명·종류·준비 상태·승인·키 등급·파일과 표 수·마지막 갱신.
export function SqlSourceCard({
  source,
  onDelete,
}: {
  source: SqlSourceSummary;
  onDelete: (source: SqlSourceSummary) => void;
}) {
  const { t } = useTranslation("sql");
  const { t: tk } = useTranslation("knowledge");
  const now = useNow();
  const navigate = useNavigate();
  const href = `/knowledge/sql-sources/${source.id}`;
  return (
    <li className="relative flex flex-col gap-3 rounded-lg border border-line bg-surface p-4 transition-colors hover:border-line-strong">
      <div className="flex items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-accent-soft text-accent">
          <Database className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <Link
            to={href}
            className="block truncate font-medium text-ink after:absolute after:inset-0 after:rounded-lg"
          >
            {source.name}
          </Link>
          <p className="mt-0.5 line-clamp-2 min-h-10 text-sm text-ink-muted">
            {source.description || " "}
          </p>
        </div>
        <Menu>
          <MenuTrigger asChild>
            <Button
              variant="ghost"
              size="iconSm"
              className="relative z-10"
              aria-label={t("list.menu", { name: source.name })}
            >
              <Ellipsis className="size-4" aria-hidden />
            </Button>
          </MenuTrigger>
          <MenuContent align="end">
            <MenuItem onSelect={() => navigate(href)}>
              <FolderOpen className="size-4" aria-hidden />
              {t("list.open")}
            </MenuItem>
            <MenuItem tone="danger" onSelect={() => onDelete(source)}>
              <Trash2 className="size-4" aria-hidden />
              {t("list.deleteTitle")}
            </MenuItem>
          </MenuContent>
        </Menu>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-subtle">
        <Badge>{t(`kind.${source.kind}`)}</Badge>
        <SqlStateBadge state={source.state} />
        {source.approval === "always" ? <Badge tone="info">{t("approval.always")}</Badge> : null}
        {source.key_tier === "paid" ? <Badge tone="warn">{tk("tier.paid")}</Badge> : null}
        <span>
          {t("list.files", { count: source.file_count })} ·{" "}
          {t("list.tables", { count: source.table_count })}
        </span>
        <span className="ml-auto">
          {t("list.updated", {
            time: formatRelative(source.updated_at, relativeBase(source.updated_at, now)),
          })}
        </span>
      </div>
    </li>
  );
}
