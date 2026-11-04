import { Ellipsis, FolderOpen, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import type { KnowledgeBaseOut } from "@/api/types";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { relativeBase } from "@/features/knowledge/documentStatus";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";

// 지식 목록의 카드(설계 12.3): 이름·설명·문서 수·청크 수·색인 방식·색인 중 수·마지막 갱신.
export function KbCard({
  kb,
  onDelete,
}: {
  kb: KnowledgeBaseOut;
  onDelete: (kb: KnowledgeBaseOut) => void;
}) {
  const { t } = useTranslation("knowledge");
  const now = useNow();
  const navigate = useNavigate();
  return (
    <li className="relative flex flex-col gap-3 rounded-lg border border-line bg-surface p-4 transition-colors hover:border-line-strong">
      <div className="flex items-start gap-3">
        <AgentIcon icon={kb.icon} name={kb.name} />
        <div className="min-w-0 flex-1">
          <Link
            to={`/knowledge/${kb.id}`}
            className="block truncate font-medium text-ink after:absolute after:inset-0 after:rounded-lg"
          >
            {kb.name}
          </Link>
          <p className="mt-0.5 line-clamp-2 min-h-10 text-sm text-ink-muted">
            {kb.description || " "}
          </p>
        </div>
        <Menu>
          <MenuTrigger asChild>
            <Button
              variant="ghost"
              size="iconSm"
              className="relative z-10"
              aria-label={t("list.menu", { name: kb.name })}
            >
              <Ellipsis className="size-4" aria-hidden />
            </Button>
          </MenuTrigger>
          <MenuContent align="end">
            <MenuItem onSelect={() => navigate(`/knowledge/${kb.id}`)}>
              <FolderOpen className="size-4" aria-hidden />
              {t("list.open")}
            </MenuItem>
            <MenuItem tone="danger" onSelect={() => onDelete(kb)}>
              <Trash2 className="size-4" aria-hidden />
              {t("list.deleteTitle")}
            </MenuItem>
          </MenuContent>
        </Menu>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-subtle">
        <Badge tone={kb.indexing_technique === "high_quality" ? "accent" : "neutral"}>
          {t(`technique.${kb.indexing_technique}`)}
        </Badge>
        <Badge>{t(`docForm.${kb.doc_form}`)}</Badge>
        {kb.data_policy.key_tier === "paid" ? <Badge tone="warn">{t("tier.paid")}</Badge> : null}
        {kb.indexing_document_count > 0 ? (
          <Badge tone="info">{t("list.indexing", { count: kb.indexing_document_count })}</Badge>
        ) : null}
        <span>
          {t("list.documents", { count: kb.document_count })} ·{" "}
          {t("list.segments", { count: kb.segment_count })}
        </span>
        <span className="ml-auto">
          {t("list.updated", {
            time: formatRelative(kb.updated_at, relativeBase(kb.updated_at, now)),
          })}
        </span>
      </div>
    </li>
  );
}
