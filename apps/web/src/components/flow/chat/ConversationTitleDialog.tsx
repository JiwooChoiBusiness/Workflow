import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import "@/features/workflows/i18n";
import { errorMessage } from "@/lib/errors";

// 서버 SessionTitle과 같은 상한
const MAX_TITLE = 200;

function TitleForm({
  initial,
  onCancel,
  onSave,
}: {
  initial: string;
  onCancel: () => void;
  onSave: (title: string) => Promise<unknown>;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const title = value.trim();
    if (!title) {
      setError(t("chat.titleRequired"));
      return;
    }
    setPending(true);
    try {
      await onSave(title);
    } catch (failure) {
      setError(errorMessage(failure, tc));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-4">
      <Field label={t("chat.titleLabel")} error={error ?? undefined} required>
        <Input
          value={value}
          maxLength={MAX_TITLE}
          onChange={(event) => {
            setValue(event.target.value);
            setError(null);
          }}
        />
      </Field>
      <DialogFooter>
        <Button onClick={onCancel}>{tc("actions.cancel")}</Button>
        <Button type="submit" variant="primary" loading={pending}>
          {tc("actions.save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

// 대화 이름 바꾸기(미리보기·웹앱 대화 목록). 저장이 끝나면 닫는 것은 onSave를 넘긴 쪽이 한다.
export function ConversationTitleDialog({
  open,
  onOpenChange,
  initial,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initial: string;
  onSave: (title: string) => Promise<unknown>;
}) {
  const { t } = useTranslation("workflows");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t("chat.renameTitle")}>
        {/* 닫히면 내용이 빠지므로 열 때마다 지금 제목으로 새로 시작한다. */}
        <TitleForm initial={initial} onCancel={() => onOpenChange(false)} onSave={onSave} />
      </DialogContent>
    </Dialog>
  );
}
