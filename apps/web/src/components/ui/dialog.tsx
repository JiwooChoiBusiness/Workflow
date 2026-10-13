import { X } from "lucide-react";
import { AlertDialog as RadixAlertDialog, Dialog as RadixDialog } from "radix-ui";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/cn";
import { Button } from "./button";

export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

const overlayClass = "fixed inset-0 z-40 bg-overlay";
const contentClass =
  "fixed top-1/2 left-1/2 z-50 max-h-[calc(100vh-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto " +
  "rounded-lg border border-line bg-surface p-5 shadow-lg focus:outline-none";

export function DialogContent({
  title,
  description,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className={overlayClass} />
      <RadixDialog.Content
        className={cn(contentClass, "w-[min(32rem,calc(100vw-2rem))]", className)}
        {...(description ? {} : { "aria-describedby": undefined })}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <RadixDialog.Title className="text-base font-semibold text-ink">
              {title}
            </RadixDialog.Title>
            {description ? (
              <RadixDialog.Description className="mt-1 text-sm text-ink-muted">
                {description}
              </RadixDialog.Description>
            ) : null}
          </div>
          <RadixDialog.Close asChild>
            <Button variant="ghost" size="iconSm" aria-label={t("actions.close")}>
              <X className="size-4" aria-hidden />
            </Button>
          </RadixDialog.Close>
        </div>
        {children}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
}

export function DialogFooter({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("mt-5 flex justify-end gap-2", className)}>{children}</div>;
}

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description: ReactNode;
  confirmLabel: ReactNode;
  onConfirm: () => void;
  tone?: "danger" | "default";
  pending?: boolean;
  error?: ReactNode;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  tone = "danger",
  pending = false,
  error,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  return (
    <RadixAlertDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixAlertDialog.Portal>
        <RadixAlertDialog.Overlay className={overlayClass} />
        <RadixAlertDialog.Content className={cn(contentClass, "w-[min(26rem,calc(100vw-2rem))]")}>
          <RadixAlertDialog.Title className="text-base font-semibold text-ink">
            {title}
          </RadixAlertDialog.Title>
          <RadixAlertDialog.Description className="mt-2 text-sm text-ink-muted">
            {description}
          </RadixAlertDialog.Description>
          {error ? <div className="mt-3 text-sm text-danger">{error}</div> : null}
          <div className="mt-5 flex justify-end gap-2">
            <RadixAlertDialog.Cancel asChild>
              <Button variant="secondary" disabled={pending}>
                {t("actions.cancel")}
              </Button>
            </RadixAlertDialog.Cancel>
            <Button
              variant={tone === "danger" ? "danger" : "primary"}
              loading={pending}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </div>
        </RadixAlertDialog.Content>
      </RadixAlertDialog.Portal>
    </RadixAlertDialog.Root>
  );
}
