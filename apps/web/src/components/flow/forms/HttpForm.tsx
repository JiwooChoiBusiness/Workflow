import { useMemo } from "react";
import { Controller, useFieldArray, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  HTTP_AUTH_TYPES,
  HTTP_BODY_TYPES,
  HTTP_METHODS,
  httpFromForm,
  httpSchema,
  httpToForm,
  type HttpForm as HttpFormValues,
} from "@/features/workflows/configSchemas";
import { toNumber } from "@/lib/formSchemas";
import { PromptEditor } from "../PromptEditor";
import {
  AddButton,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

type KeyValueList = "params" | "headers" | "body_form";

// 이름·값 목록(쿼리, 헤더, 폼 본문). 값은 템플릿이라 {{ }}로 변수를 넣는다.
function KeyValueRows({
  form,
  name,
  nodeId,
  title,
  serverPath,
  serverIssue,
}: {
  form: UseFormReturn<HttpFormValues>;
  name: KeyValueList;
  nodeId: string;
  title: string;
  // 서버 이슈의 경로(form 본문은 body.content[i])
  serverPath: string;
  serverIssue: (path: string) => string | undefined;
}) {
  const { t } = useTranslation("workflows");
  const { fields, append, remove } = useFieldArray({ control: form.control, name });
  return (
    <Section
      title={title}
      actions={
        <AddButton label={t("forms.http.add")} onClick={() => append({ name: "", value: "" })} />
      }
    >
      {fields.map((field, index) => {
        const row = t("common.row", { n: index + 1 });
        const error =
          errorAt(form, `${name}.${index}.name`, serverIssue) ??
          serverIssue(`${serverPath}.${index}.name`) ??
          serverIssue(`${serverPath}.${index}.value`);
        return (
          <div key={field.id} className="flex flex-col gap-1">
            <div className="flex items-start gap-1.5">
              <Input
                className="w-32 shrink-0 font-mono"
                placeholder={t("common.name")}
                aria-label={`${title} ${row} ${t("common.name")}`}
                {...form.register(`${name}.${index}.name`)}
              />
              <div className="min-w-0 flex-1">
                <Controller
                  control={form.control}
                  name={`${name}.${index}.value`}
                  render={({ field: control }) => (
                    <PromptEditor
                      nodeId={nodeId}
                      value={control.value}
                      onChange={control.onChange}
                      ariaLabel={`${title} ${row} ${t("common.value")}`}
                      singleLine
                    />
                  )}
                />
              </div>
              <RemoveButton
                label={t("common.remove", { name: row })}
                onClick={() => remove(index)}
              />
            </div>
            <FieldMessage message={error} />
          </div>
        );
      })}
    </Section>
  );
}

export function HttpForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => httpSchema(tc, t), [tc, t]);
  const form = useNodeForm<HttpFormValues>(node, schema, httpToForm, httpFromForm);
  const serverIssue = useServerIssue(node.id);
  const bodyType = useWatch({ control: form.control, name: "body_type" });
  const authType = useWatch({ control: form.control, name: "auth.type" });

  return (
    <div className="flex flex-col gap-4">
      <Alert tone="warn" title={t("forms.http.injection")} />
      <div className="flex items-start gap-1.5">
        <Field className="w-28 shrink-0" label={t("forms.http.method")}>
          <Controller
            control={form.control}
            name="method"
            render={({ field }) => (
              <Select
                value={field.value}
                onValueChange={field.onChange}
                options={HTTP_METHODS.map((method) => ({ value: method, label: method }))}
              />
            )}
          />
        </Field>
        <Field
          className="min-w-0 flex-1"
          label={t("forms.http.url")}
          error={errorAt(form, "url", serverIssue)}
          required
        >
          <Controller
            control={form.control}
            name="url"
            render={({ field }) => (
              <PromptEditor
                nodeId={node.id}
                value={field.value}
                onChange={field.onChange}
                ariaLabel={t("forms.http.url")}
                placeholder="https://"
                singleLine
              />
            )}
          />
        </Field>
      </div>
      <p className="-mt-2 text-xs text-ink-muted">{t("forms.http.urlHint")}</p>
      <KeyValueRows
        form={form}
        name="params"
        nodeId={node.id}
        title={t("forms.http.params")}
        serverPath="params"
        serverIssue={serverIssue}
      />
      <KeyValueRows
        form={form}
        name="headers"
        nodeId={node.id}
        title={t("forms.http.headers")}
        serverPath="headers"
        serverIssue={serverIssue}
      />
      <Section title={t("forms.http.body")}>
        <Controller
          control={form.control}
          name="body_type"
          render={({ field }) => (
            <Select
              aria-label={t("forms.http.body")}
              value={field.value}
              onValueChange={field.onChange}
              options={HTTP_BODY_TYPES.map((type) => ({
                value: type,
                label: t(`forms.http.bodyTypes.${type}`),
                // 서버가 아직 파일 본문을 보내지 못한다(unsupported_body_type).
                disabled: type === "file" && field.value !== "file",
              }))}
            />
          )}
        />
        <FieldMessage message={serverIssue("body.type")} />
        {bodyType === "json" || bodyType === "raw" ? (
          <Field
            hint={
              bodyType === "json"
                ? t("forms.http.jsonHint", { example: "{{ start.q | tojson }}" })
                : undefined
            }
            error={serverIssue("body.content")}
          >
            <Controller
              control={form.control}
              name="body_text"
              render={({ field }) => (
                <PromptEditor
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  ariaLabel={t("forms.http.body")}
                  minHeight={120}
                />
              )}
            />
          </Field>
        ) : null}
      </Section>
      {bodyType === "form" ? (
        <KeyValueRows
          form={form}
          name="body_form"
          nodeId={node.id}
          title={t("forms.http.bodyTypes.form")}
          serverPath="body.content"
          serverIssue={serverIssue}
        />
      ) : null}
      <Section title={t("forms.http.auth")}>
        <Controller
          control={form.control}
          name="auth.type"
          render={({ field }) => (
            <Select
              aria-label={t("forms.http.auth")}
              value={field.value}
              onValueChange={field.onChange}
              options={HTTP_AUTH_TYPES.map((type) => ({
                value: type,
                label: t(`forms.http.authTypes.${type}`),
              }))}
            />
          )}
        />
        {authType === "header" ? (
          <Field
            label={t("forms.http.authHeader")}
            error={errorAt(form, "auth.header", serverIssue)}
          >
            <Input className="font-mono" {...form.register("auth.header")} />
          </Field>
        ) : null}
        {authType !== "none" ? (
          <Field
            label={t("forms.http.authValue")}
            hint={t("forms.http.authValueHint", { example: "{{ env.API_TOKEN }}" })}
            error={serverIssue("auth.value")}
          >
            <Controller
              control={form.control}
              name="auth.value"
              render={({ field }) => (
                <PromptEditor
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  ariaLabel={t("forms.http.authValue")}
                  singleLine
                />
              )}
            />
          </Field>
        ) : null}
      </Section>
      <div className="grid grid-cols-2 gap-3">
        <Field
          label={t("forms.http.timeout")}
          hint={t("forms.http.timeoutHint")}
          error={errorAt(form, "timeout_sec", serverIssue)}
        >
          <Input
            type="number"
            step="1"
            {...form.register("timeout_sec", { setValueAs: toNumber })}
          />
        </Field>
        <Field
          label={t("forms.http.maxBody")}
          hint={t("forms.http.maxBodyHint")}
          error={errorAt(form, "max_body_bytes", serverIssue)}
        >
          <Input
            type="number"
            step="1"
            {...form.register("max_body_bytes", { setValueAs: toNumber })}
          />
        </Field>
      </div>
    </div>
  );
}
