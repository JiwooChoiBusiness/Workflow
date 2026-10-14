import { Play, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { FlowField, WfFileOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import "@/features/workflows/i18n";
import type { FieldValue } from "@/features/workflows/runInputs";
import { RunInputsForm } from "../run/RunInputsForm";

// 웹앱의 입력 폼(설계 7.4 FlowRunForm): 발행 버전의 start 필드, 실행·중지.
export function FlowRunForm({
  fields,
  values,
  onChange,
  errors,
  upload,
  running,
  starting,
  stopping,
  onRun,
  onStop,
}: {
  fields: readonly FlowField[];
  values: Readonly<Record<string, FieldValue>>;
  onChange: (name: string, value: FieldValue) => void;
  errors: Readonly<Record<string, string>>;
  upload: (file: File) => Promise<WfFileOut>;
  running: boolean;
  starting: boolean;
  stopping: boolean;
  onRun: () => void;
  onStop: () => void;
}) {
  const { t } = useTranslation("workflows");
  return (
    <form
      className="flex flex-col gap-4"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onRun();
      }}
    >
      <RunInputsForm
        fields={fields}
        values={values}
        onChange={onChange}
        errors={errors}
        upload={upload}
        disabled={running || starting}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" loading={starting} disabled={running}>
          <Play className="size-3.5" aria-hidden />
          {t("run.start")}
        </Button>
        {running ? (
          <Button loading={stopping} onClick={onStop}>
            <Square className="size-3.5" aria-hidden />
            {t("run.stop")}
          </Button>
        ) : null}
      </div>
    </form>
  );
}
