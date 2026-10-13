import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export function AuthCard({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-full items-center justify-center bg-canvas p-6">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center gap-2">
          <span className="grid size-8 place-items-center rounded-md bg-rail text-sm font-bold text-rail-mark">
            W
          </span>
          <span className="text-sm font-semibold text-ink">{t("appName")}</span>
        </div>
        <main className="rounded-lg border border-line bg-surface p-6 shadow-sm">
          <h1 className="text-lg font-semibold text-ink">{title}</h1>
          {description ? <p className="mt-1 text-sm text-ink-muted">{description}</p> : null}
          <div className="mt-5">{children}</div>
        </main>
      </div>
    </div>
  );
}
