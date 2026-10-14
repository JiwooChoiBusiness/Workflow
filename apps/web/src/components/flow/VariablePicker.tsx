import { ChevronDown, Search, X } from "lucide-react";
import { Popover } from "radix-ui";
import { useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { VarType } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useFieldControl } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { assignable, parseSelector } from "@/features/workflows/graph";
import { nodeMeta, TONE_CLASS } from "@/features/workflows/nodeTypes";
import {
  useVariableGroups,
  type PickerScope,
  type VarGroup,
} from "@/features/workflows/useUpstream";
import { cn } from "@/lib/cn";

function fits(type: VarType, accept: readonly VarType[] | undefined): boolean {
  if (!accept || accept.length === 0) return true;
  return accept.some((target) => assignable(type, target) !== "no");
}

// 변수 목록(상류 노드 출력·sys·env·conv). 선택기 팝업과 프롬프트 편집기의 {{ 팝업이 같이 쓴다.
export function VariableList({
  groups,
  onPick,
  accept,
  allowCustom = true,
}: {
  groups: VarGroup[];
  onPick: (selector: string) => void;
  accept?: readonly VarType[];
  allowCustom?: boolean;
}) {
  const { t } = useTranslation("workflows");
  const [query, setQuery] = useState("");
  const [custom, setCustom] = useState("");
  // 팝업은 포털이지만 React 컨텍스트는 이어진다. 선택기를 감싼 Field의 id·오류 표시가 이 칸들로 새지 않게
  // 직접 정한다.
  const searchId = useId();
  const customId = useId();
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      groups
        .map((group) => ({
          ...group,
          vars: group.vars.filter(
            (item) =>
              !needle ||
              item.selector.toLowerCase().includes(needle) ||
              group.title.toLowerCase().includes(needle),
          ),
        }))
        .filter((group) => group.vars.length > 0),
    [groups, needle],
  );
  const customValid = parseSelector(custom) !== null;

  return (
    <div className="flex max-h-[min(26rem,var(--radix-popover-content-available-height,26rem))] w-80 flex-col">
      <div className="relative border-b border-line p-2">
        <Search
          className="pointer-events-none absolute top-4 left-4 size-4 text-ink-subtle"
          aria-hidden
        />
        {/* 팝업이 열리면 Radix가 첫 칸(이 검색 칸)에 초점을 둔다. */}
        <Input
          id={searchId}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("picker.search")}
          aria-label={t("picker.search")}
          aria-invalid={false}
          aria-describedby=""
          aria-required={false}
          className="pl-8"
          onKeyDown={(event) => {
            // 찾아서 Enter를 누르면 첫 변수를 고른다(프롬프트 편집기의 {{ 팝업에서 손을 떼지 않게).
            const first = visible[0]?.vars[0];
            if (event.key === "Enter" && first) {
              event.preventDefault();
              onPick(first.selector);
            }
          }}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-1">
        {visible.length === 0 ? (
          <p className="px-2 py-4 text-center text-sm text-ink-muted">{t("picker.none")}</p>
        ) : (
          visible.map((group) => {
            const meta = group.nodeType ? nodeMeta(group.nodeType) : null;
            const Icon = meta?.icon;
            return (
              <div key={group.key} role="group" aria-label={group.title} className="py-1">
                <p className="flex items-center gap-1.5 px-2 pb-0.5 text-xs font-medium text-ink-subtle">
                  {meta && Icon ? (
                    <span
                      className={cn(
                        "grid size-4 place-items-center rounded-sm",
                        TONE_CLASS[meta.tone],
                      )}
                    >
                      <Icon className="size-3" aria-hidden />
                    </span>
                  ) : null}
                  <span className="truncate">{group.title}</span>
                  {group.nodeType ? <span className="font-mono">{group.scope}</span> : null}
                </p>
                {group.vars.map((item) => {
                  const ok = fits(item.type, accept);
                  return (
                    <button
                      key={item.selector}
                      type="button"
                      onClick={() => onPick(item.selector)}
                      title={ok ? item.selector : t("picker.mismatch")}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-sm hover:bg-hover",
                        ok ? "text-ink" : "text-ink-subtle",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate font-mono">{item.name}</span>
                      <Badge tone={ok ? "neutral" : "warn"} className="font-mono">
                        {item.type}
                      </Badge>
                    </button>
                  );
                })}
              </div>
            );
          })
        )}
      </div>
      {allowCustom ? (
        <form
          className="flex flex-col gap-1 border-t border-line p-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (customValid) onPick(custom.trim());
          }}
        >
          <div className="flex gap-1.5">
            <Input
              id={customId}
              value={custom}
              onChange={(event) => setCustom(event.target.value)}
              placeholder="node_id.output"
              aria-label={t("picker.custom")}
              aria-invalid={custom !== "" && !customValid}
              aria-describedby={`${customId}-hint`}
              aria-required={false}
              className="font-mono"
            />
            <Button type="submit" size="md" disabled={!customValid}>
              {t("picker.apply")}
            </Button>
          </div>
          <p id={`${customId}-hint`} className="text-xs text-ink-muted">
            {custom && !customValid ? t("picker.invalid") : t("picker.customHint")}
          </p>
        </form>
      ) : null}
    </div>
  );
}

function describe(groups: VarGroup[], value: string) {
  const selector = parseSelector(value);
  if (!selector) return null;
  for (const group of groups) {
    const found = group.vars.find((item) => item.selector === value.trim());
    if (found) return { group, item: found };
  }
  return null;
}

export interface VariablePickerProps {
  nodeId: string;
  value: string;
  onChange: (value: string) => void;
  scope?: PickerScope;
  accept?: readonly VarType[];
  id?: string;
  "aria-label"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
  placeholder?: string;
  clearable?: boolean;
  className?: string;
}

// 구조화 필드(변수 하나를 고르는 칸)의 선택기. 값은 "node_id.output" 같은 선택자 문자열이다(설계 3.2).
export function VariablePicker({
  nodeId,
  value,
  onChange,
  scope = "upstream",
  accept,
  placeholder,
  clearable = false,
  className,
  ...props
}: VariablePickerProps) {
  const { t } = useTranslation("workflows");
  const field = useFieldControl(props);
  const groups = useVariableGroups(nodeId, scope);
  const [open, setOpen] = useState(false);
  const found = describe(groups, value);
  const text = value.trim();
  const mismatch = found !== null && !fits(found.item.type, accept);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <div className={cn("flex min-w-0 items-center gap-1", className)}>
        <Popover.Trigger asChild>
          <button
            type="button"
            {...field}
            aria-label={props["aria-label"]}
            className={cn(
              "flex h-8 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 text-left text-sm",
              "transition-colors hover:border-line-strong focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
              "aria-invalid:border-danger",
            )}
          >
            {text ? (
              <>
                {found ? (
                  <span className="truncate text-xs text-ink-muted">{found.group.title}</span>
                ) : null}
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate font-mono text-[13px]",
                    found ? "text-ink" : "text-warn",
                  )}
                  title={found ? text : t("picker.unknown")}
                >
                  {found ? found.item.name : text}
                </span>
                {found ? (
                  <Badge tone={mismatch ? "warn" : "neutral"} className="font-mono">
                    {found.item.type}
                  </Badge>
                ) : null}
              </>
            ) : (
              <span className="flex-1 truncate text-ink-subtle">
                {placeholder ?? t("picker.placeholder")}
              </span>
            )}
            <ChevronDown className="size-4 shrink-0 text-ink-subtle" aria-hidden />
          </button>
        </Popover.Trigger>
        {clearable && text ? (
          <Button
            size="iconSm"
            variant="ghost"
            aria-label={t("picker.clear")}
            title={t("picker.clear")}
            onClick={() => onChange("")}
          >
            <X className="size-3.5" aria-hidden />
          </Button>
        ) : null}
      </div>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          collisionPadding={8}
          className="z-50 overflow-hidden rounded-md border border-line bg-surface shadow-lg"
        >
          <VariableList
            groups={groups}
            accept={accept}
            onPick={(selector) => {
              onChange(selector);
              setOpen(false);
            }}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
