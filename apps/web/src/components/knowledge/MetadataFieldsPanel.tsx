import { Pencil, Plus, Tags, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { MetadataFieldOut, MetadataType } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import {
  useCreateField,
  useDeleteField,
  useMetadataFields,
  useRenameField,
  useToggleBuiltIn,
} from "@/features/knowledge/api";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatNumber } from "@/lib/format";
import { KbErrorAlert } from "./KbErrorAlert";
import { NameDialog } from "./NameDialog";

// 서버 FieldName(app/schemas/knowledge.py)과 같은 규칙.
const FIELD_NAME = /^[a-z][a-z0-9_]{0,39}$/;
const TYPES: readonly MetadataType[] = ["string", "number", "time"];

// 메타데이터 탭(설계 7.5, 12.3): 필드 관리와 기본 필드 스위치. 값은 문서 표의 일괄 편집과 문서 화면에서 넣는다.
export function MetadataFieldsPanel({ kbId }: { kbId: string }) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const fields = useMetadataFields(kbId);
  const create = useCreateField(kbId);
  const rename = useRenameField(kbId);
  const remove = useDeleteField(kbId);
  const builtIn = useToggleBuiltIn(kbId);
  const [name, setName] = useState("");
  const [type, setType] = useState<MetadataType>("string");
  const [nameError, setNameError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<MetadataFieldOut | null>(null);
  const [deleting, setDeleting] = useState<MetadataFieldOut | null>(null);

  if (fields.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (fields.isError) {
    return (
      <KbErrorAlert
        error={fields.error}
        title={t("metadata.loadFailed")}
        onRetry={() => void fields.refetch()}
      />
    );
  }
  const items = fields.data.items;

  function add() {
    const trimmed = name.trim();
    if (!FIELD_NAME.test(trimmed)) {
      setNameError(t("metadata.nameInvalid"));
      return;
    }
    setNameError(null);
    create.mutate(
      { name: trimmed, type },
      {
        onSuccess: () => {
          toast.success(t("metadata.added"));
          setName("");
        },
        onError: (error) => setNameError(errorText(error)),
      },
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel title={t("metadata.builtIn")} description={t("metadata.builtInHint")}>
        <Field label={t("metadata.builtIn")} inline>
          <Switch
            checked={fields.data.built_in_fields_enabled}
            disabled={builtIn.isPending}
            onCheckedChange={(checked) =>
              builtIn.mutate(checked, {
                onSuccess: () =>
                  toast.success(checked ? t("metadata.builtInOn") : t("metadata.builtInOff")),
                onError: (error) => toast.error(errorText(error)),
              })
            }
          />
        </Field>
      </Panel>
      <Panel title={t("metadata.title")} description={t("metadata.description")}>
        <form
          className="mb-4 flex flex-wrap items-end gap-2"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            add();
          }}
        >
          <Field
            label={t("metadata.name")}
            hint={nameError ? undefined : t("metadata.nameHint")}
            error={nameError ?? undefined}
            className="min-w-56 flex-1"
          >
            <Input
              value={name}
              className="font-mono"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label={t("metadata.type")} className="w-32">
            <Select
              value={type}
              onValueChange={(value) => setType(value as MetadataType)}
              options={TYPES.map((value) => ({ value, label: t(`metadata.types.${value}`) }))}
            />
          </Field>
          <Button type="submit" loading={create.isPending} className="mb-[1.375rem]">
            <Plus className="size-4" aria-hidden />
            {t("metadata.add")}
          </Button>
        </form>
        {items.length === 0 ? (
          <EmptyState icon={Tags} title={t("metadata.empty")} />
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-ink-muted">
              <tr>
                <th className="pb-2 font-medium">{t("metadata.column.name")}</th>
                <th className="pb-2 font-medium">{t("metadata.column.type")}</th>
                <th className="pb-2 text-right font-medium">{t("metadata.column.uses")}</th>
                <th className="pb-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.map((field) => (
                <tr key={field.id}>
                  <td className="py-2 pr-3">
                    <span className="font-mono">{field.name}</span>
                    {field.built_in ? (
                      <Badge className="ml-2">{t("metadata.builtInBadge")}</Badge>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3">{t(`metadata.types.${field.type}`)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {formatNumber(field.use_count)}
                  </td>
                  <td className="py-1.5 text-right whitespace-nowrap">
                    {field.built_in ? null : (
                      <>
                        <Button
                          size="iconSm"
                          variant="ghost"
                          aria-label={`${t("metadata.rename")}: ${field.name}`}
                          title={t("metadata.rename")}
                          onClick={() => setRenaming(field)}
                        >
                          <Pencil className="size-3.5" aria-hidden />
                        </Button>
                        <Button
                          size="iconSm"
                          variant="dangerGhost"
                          aria-label={`${t("metadata.delete")}: ${field.name}`}
                          title={t("metadata.delete")}
                          onClick={() => setDeleting(field)}
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {renaming ? (
        <NameDialog
          key={renaming.id}
          open
          onOpenChange={(open) => {
            if (!open) setRenaming(null);
          }}
          title={t("metadata.renameTitle")}
          label={t("metadata.name")}
          hint={t("metadata.nameHint")}
          initial={renaming.name}
          check={(value) => (FIELD_NAME.test(value) ? null : t("metadata.nameInvalid"))}
          pending={rename.isPending}
          error={rename.error ? errorText(rename.error) : null}
          onSubmit={(value) =>
            rename.mutate(
              { fieldId: renaming.id, name: value },
              {
                onSuccess: () => {
                  toast.success(t("metadata.renamed"));
                  setRenaming(null);
                },
              },
            )
          }
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("metadata.deleteTitle")}
        description={t("metadata.deleteBody", { name: deleting?.name ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("metadata.deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorText(error)),
          });
        }}
      />
    </div>
  );
}
