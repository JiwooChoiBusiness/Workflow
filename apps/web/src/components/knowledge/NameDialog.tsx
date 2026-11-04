import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

// 문서·메타데이터 필드의 이름 바꾸기. 검사(check)가 문구를 돌려주면 보내지 않는다. 열 때 처음 값으로
// 돌아가도록 부르는 쪽이 대상마다 key를 바꿔 다시 만든다.
export function NameDialog({
  open,
  onOpenChange,
  title,
  label,
  hint,
  initial,
  check,
  onSubmit,
  pending,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  label: string;
  hint?: string;
  initial: string;
  check?: (value: string) => string | null;
  onSubmit: (value: string) => void;
  pending?: boolean;
  error?: string | null;
}) {
  const { t: tc } = useTranslation();
  const [value, setValue] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        setProblem(null);
      }}
    >
      <DialogContent title={title}>
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = value.trim();
            const found = !trimmed ? tc("validation.required") : (check?.(trimmed) ?? null);
            setProblem(found);
            if (!found) onSubmit(trimmed);
          }}
        >
          <Field label={label} hint={hint} error={problem ?? undefined} required>
            <Input value={value} onChange={(event) => setValue(event.target.value)} />
          </Field>
          {error ? <Alert tone="danger" title={error} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button type="submit" variant="primary" loading={pending}>
              {tc("actions.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
