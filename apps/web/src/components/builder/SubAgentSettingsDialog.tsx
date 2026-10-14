import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { RadioCards } from "@/components/ui/radio-cards";
import { Switch } from "@/components/ui/toggle";
import { schemaTextProblem, type SubAgentForm } from "@/features/agents/subAgents";

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-sm font-medium text-ink">{label}</p>
      {children}
    </div>
  );
}

function SettingsBody({
  value,
  serverError,
  onApply,
  onCancel,
}: {
  value: SubAgentForm;
  serverError?: string;
  onApply: (next: SubAgentForm) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const [draft, setDraft] = useState(value);
  const [problem, setProblem] = useState<string | null>(null);
  const update = (patch: Partial<SubAgentForm>) =>
    setDraft((current) => ({ ...current, ...patch }));
  const fixedSchema = draft.format === "json" && draft.schema_source === "fixed";
  // 편집기를 고치기 전까지는 저장할 때 서버가 돌려준 스키마 오류를 같이 보여 준다.
  const schemaError =
    problem ?? (draft.schema_text === value.schema_text ? serverError : undefined);

  function apply() {
    if (fixedSchema) {
      const found = schemaTextProblem(draft.schema_text);
      if (found) {
        setProblem(tc(`validation.${found}`));
        return;
      }
    }
    onApply(draft);
  }

  return (
    <div className="flex flex-col gap-5">
      <Group label={t("subAgents.dialog.format")}>
        <RadioCards
          ariaLabel={t("subAgents.dialog.format")}
          value={draft.format}
          onValueChange={(format) => update({ format: format as SubAgentForm["format"] })}
          options={[
            {
              value: "text",
              label: t("subAgents.dialog.formatText"),
              description: t("subAgents.dialog.formatTextHint"),
            },
            {
              value: "json",
              label: t("subAgents.dialog.formatJson"),
              description: t("subAgents.dialog.formatJsonHint"),
            },
          ]}
        />
      </Group>
      {draft.format === "json" ? (
        <Group label={t("subAgents.dialog.schemaSource")}>
          <RadioCards
            ariaLabel={t("subAgents.dialog.schemaSource")}
            value={draft.schema_source}
            onValueChange={(source) =>
              update({ schema_source: source as SubAgentForm["schema_source"] })
            }
            options={[
              {
                value: "fixed",
                label: t("subAgents.dialog.schemaFixed"),
                description: t("subAgents.dialog.schemaFixedHint"),
              },
              {
                value: "model",
                label: t("subAgents.dialog.schemaModel"),
                description: t("subAgents.dialog.schemaModelHint"),
              },
            ]}
          />
        </Group>
      ) : null}
      {fixedSchema ? (
        <Field
          label={t("subAgents.dialog.schema")}
          hint={t("subAgents.dialog.schemaHint")}
          error={schemaError}
        >
          <CodeEditor
            value={draft.schema_text}
            onChange={(schema_text) => {
              setProblem(null);
              update({ schema_text });
            }}
            language="json"
            ariaLabel={t("subAgents.dialog.schema")}
            minHeight={160}
            maxHeight={320}
          />
        </Field>
      ) : null}
      <Field inline label={t("subAgents.dialog.files")} hint={t("subAgents.dialog.filesHint")}>
        <Switch checked={draft.files} onCheckedChange={(files) => update({ files })} />
      </Field>
      <Group label={t("subAgents.dialog.approval")}>
        <RadioCards
          ariaLabel={t("subAgents.dialog.approval")}
          value={draft.approval_mode}
          onValueChange={(mode) => update({ approval_mode: mode as SubAgentForm["approval_mode"] })}
          options={[
            {
              value: "inherit",
              label: t("subAgents.dialog.approvalInherit"),
              description: t("subAgents.dialog.approvalInheritHint"),
            },
            {
              value: "off",
              label: t("subAgents.dialog.approvalOff"),
              description: t("subAgents.dialog.approvalOffHint"),
            },
          ]}
        />
      </Group>
      {draft.approval_mode === "off" ? (
        <Alert tone="danger" title={t("approval.offWarning")} />
      ) : null}
      <DialogFooter className="mt-0">
        <Button variant="secondary" onClick={onCancel}>
          {tc("actions.cancel")}
        </Button>
        <Button variant="primary" onClick={apply}>
          {t("subAgents.dialog.apply")}
        </Button>
      </DialogFooter>
    </div>
  );
}

// 하위 에이전트 항목 하나의 호출 설정(오케스트레이션 2.2, 8.2). 적용해야 폼에 들어가고, 저장은 빌더의 저장 버튼이다.
export function SubAgentSettingsDialog({
  open,
  onOpenChange,
  agentName,
  value,
  serverError,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agentName: string;
  value: SubAgentForm | null;
  serverError?: string;
  onApply: (next: SubAgentForm) => void;
}) {
  const { t } = useTranslation("agents");
  return (
    <Dialog open={open && value !== null} onOpenChange={onOpenChange}>
      <DialogContent
        title={t("subAgents.dialog.title", { name: agentName })}
        description={t("subAgents.dialog.description")}
        className="w-[min(40rem,calc(100vw-2rem))]"
      >
        {value ? (
          <SettingsBody
            key={value.agent_id}
            value={value}
            serverError={serverError}
            onApply={(next) => {
              onApply(next);
              onOpenChange(false);
            }}
            onCancel={() => onOpenChange(false)}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
