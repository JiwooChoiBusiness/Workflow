import { CircleAlert, CircleCheck, X } from "lucide-react";
import { Toast as RadixToast } from "radix-ui";
import { useTranslation } from "react-i18next";
import { create } from "zustand";
import { cn } from "@/lib/cn";

interface ToastItem {
  id: number;
  tone: "success" | "error";
  message: string;
}

interface ToastState {
  items: ToastItem[];
  push: (tone: ToastItem["tone"], message: string) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

const useToastStore = create<ToastState>()((set) => ({
  items: [],
  push: (tone, message) =>
    set((state) => ({ items: [...state.items.slice(-3), { id: nextId++, tone, message }] })),
  dismiss: (id) => set((state) => ({ items: state.items.filter((item) => item.id !== id) })),
}));

export const toast = {
  success: (message: string) => useToastStore.getState().push("success", message),
  error: (message: string) => useToastStore.getState().push("error", message),
};

export function Toaster() {
  const { t } = useTranslation();
  const items = useToastStore((state) => state.items);
  const dismiss = useToastStore((state) => state.dismiss);
  return (
    <RadixToast.Provider duration={4000} label={t("toast.region")}>
      {items.map((item) => {
        const Icon = item.tone === "success" ? CircleCheck : CircleAlert;
        return (
          <RadixToast.Root
            key={item.id}
            type={item.tone === "error" ? "foreground" : "background"}
            duration={item.tone === "error" ? 8000 : 4000}
            onOpenChange={(open) => {
              if (!open) dismiss(item.id);
            }}
            className="flex w-80 items-start gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 text-sm shadow-lg"
          >
            <Icon
              className={cn(
                "mt-0.5 size-4 shrink-0",
                item.tone === "success" ? "text-ok" : "text-danger",
              )}
              aria-hidden
            />
            <RadixToast.Description className="min-w-0 flex-1 text-ink">
              {item.message}
            </RadixToast.Description>
            <RadixToast.Close
              className="rounded-sm p-0.5 text-ink-subtle hover:text-ink"
              aria-label={t("actions.close")}
            >
              <X className="size-3.5" aria-hidden />
            </RadixToast.Close>
          </RadixToast.Root>
        );
      })}
      <RadixToast.Viewport className="fixed right-4 bottom-4 z-[60] flex flex-col gap-2 outline-none" />
    </RadixToast.Provider>
  );
}
