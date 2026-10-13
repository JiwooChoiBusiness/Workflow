import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, Trash2 } from "lucide-react";
import { useMemo } from "react";
import { useFieldArray, useForm, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { WorkspaceOut } from "@/api/types";
import { ApprovalModeSelect } from "@/components/builder/ApprovalLimitsTab";
import { LimitsFields } from "@/components/builder/LimitsFields";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageHeader, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { usePatchWorkspace, useWorkspace } from "@/features/settings/api";
import {
  formToWorkspacePatch,
  makeWorkspaceSettingsSchema,
  workspaceToForm,
  type WorkspaceSettingsForm,
} from "@/features/settings/schemas";
import { errorMessage } from "@/lib/errors";
import { toNumber, toNumberOrNull } from "@/lib/formSchemas";

function WorkspaceForm({ workspace }: { workspace: WorkspaceOut }) {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const patch = usePatchWorkspace();
  const schema = useMemo(() => makeWorkspaceSettingsSchema(tc), [tc]);
  const form = useForm<WorkspaceSettingsForm>({
    resolver: zodResolver(schema),
    defaultValues: workspaceToForm(workspace),
  });
  const prices = useFieldArray({ control: form.control, name: "model_prices" });
  const approvalMode = useWatch({ control: form.control, name: "approval_mode_default" });
  const { errors, isDirty, isSubmitting } = form.formState;

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const saved = await patch.mutateAsync(formToWorkspacePatch(values));
      form.reset(workspaceToForm(saved));
      toast.success(t("workspace.saved"));
    } catch (error) {
      toast.error(errorMessage(error, tc));
    }
  });

  return (
    <form className="flex flex-col gap-6" onSubmit={onSubmit} noValidate>
      <Panel title={t("workspace.basics")}>
        <div className="flex flex-col gap-4">
          <Field label={t("workspace.name")} error={errors.name?.message} required>
            <Input {...form.register("name")} />
          </Field>
          <Field
            label={t("workspace.retention")}
            hint={t("workspace.retentionHint")}
            error={errors.run_event_retention_days?.message}
          >
            <Input
              type="number"
              step="1"
              className="w-40"
              {...form.register("run_event_retention_days", { setValueAs: toNumber })}
            />
          </Field>
        </div>
      </Panel>
      <Panel title={t("workspace.approvalDefault")}>
        <ApprovalModeSelect
          value={approvalMode}
          onChange={(mode) => form.setValue("approval_mode_default", mode, { shouldDirty: true })}
        />
      </Panel>
      <Panel title={t("workspace.limits")}>
        <LimitsFields form={form} prefix="default_limits" />
      </Panel>
      <Panel
        title={t("workspace.prices")}
        description={t("workspace.pricesHint")}
        actions={
          <Button
            size="sm"
            onClick={() =>
              prices.append({
                model: "",
                input_per_mtok: 0,
                output_per_mtok: 0,
                cached_input_per_mtok: null,
              })
            }
          >
            <Plus className="size-3.5" aria-hidden />
            {t("workspace.addPrice")}
          </Button>
        }
      >
        {prices.fields.length === 0 ? (
          <p className="text-sm text-ink-muted">{t("workspace.noPrices")}</p>
        ) : (
          <div className="flex flex-col gap-2">
            <div className="grid grid-cols-[minmax(0,1fr)_7rem_7rem_7rem_2rem] gap-2 text-xs font-medium text-ink-muted">
              <span>{t("workspace.priceModel")}</span>
              <span>{t("workspace.priceInput")}</span>
              <span>{t("workspace.priceOutput")}</span>
              <span>{t("workspace.priceCached")}</span>
              <span />
            </div>
            {prices.fields.map((field, index) => {
              const rowErrors = errors.model_prices?.[index];
              return (
                <div key={field.id} className="flex flex-col gap-1">
                  <div className="grid grid-cols-[minmax(0,1fr)_7rem_7rem_7rem_2rem] items-center gap-2">
                    <Input
                      className="font-mono"
                      aria-label={`${t("workspace.priceModel")} ${index + 1}`}
                      aria-invalid={rowErrors?.model ? true : undefined}
                      {...form.register(`model_prices.${index}.model`)}
                    />
                    <Input
                      type="number"
                      step="any"
                      aria-label={`${t("workspace.priceInput")} ${index + 1}`}
                      aria-invalid={rowErrors?.input_per_mtok ? true : undefined}
                      {...form.register(`model_prices.${index}.input_per_mtok`, {
                        setValueAs: toNumber,
                      })}
                    />
                    <Input
                      type="number"
                      step="any"
                      aria-label={`${t("workspace.priceOutput")} ${index + 1}`}
                      aria-invalid={rowErrors?.output_per_mtok ? true : undefined}
                      {...form.register(`model_prices.${index}.output_per_mtok`, {
                        setValueAs: toNumber,
                      })}
                    />
                    <Input
                      type="number"
                      step="any"
                      aria-label={`${t("workspace.priceCached")} ${index + 1}`}
                      aria-invalid={rowErrors?.cached_input_per_mtok ? true : undefined}
                      {...form.register(`model_prices.${index}.cached_input_per_mtok`, {
                        setValueAs: toNumberOrNull,
                      })}
                    />
                    <Button
                      variant="dangerGhost"
                      size="iconSm"
                      aria-label={t("workspace.removePrice", {
                        model: form.getValues(`model_prices.${index}.model`) || index + 1,
                      })}
                      onClick={() => prices.remove(index)}
                    >
                      <Trash2 className="size-4" aria-hidden />
                    </Button>
                  </div>
                  {rowErrors ? (
                    <p className="text-xs text-danger" role="alert">
                      {rowErrors.model?.message ??
                        rowErrors.input_per_mtok?.message ??
                        rowErrors.output_per_mtok?.message ??
                        rowErrors.cached_input_per_mtok?.message}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </Panel>
      <div className="sticky bottom-0 flex items-center justify-end gap-3 border-t border-line bg-canvas py-3">
        {isDirty ? <Badge tone="warn">{tc("state.unsaved")}</Badge> : null}
        <Button type="submit" variant="primary" loading={isSubmitting} disabled={!isDirty}>
          {tc("actions.save")}
        </Button>
      </div>
    </form>
  );
}

export default function WorkspaceSettingsPage() {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const workspace = useWorkspace();
  return (
    <div>
      <PageHeader title={t("workspace.title")} description={t("workspace.description")} />
      {workspace.isPending ? (
        <PageSpinner label={tc("state.loading")} />
      ) : workspace.isError ? (
        <ErrorAlert
          error={workspace.error}
          title={t("workspace.loadFailed")}
          onRetry={() => void workspace.refetch()}
        />
      ) : (
        <WorkspaceForm key={workspace.data.updated_at} workspace={workspace.data} />
      )}
    </div>
  );
}
