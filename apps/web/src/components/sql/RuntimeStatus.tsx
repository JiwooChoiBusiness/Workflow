import { CircleCheck, Download, PackageOpen } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useInstallSqlRuntime, useSqlRuntime } from "@/features/sql/api";
import { useSqlError } from "@/features/sql/useSqlError";
import { formatBytes, formatDateTime } from "@/lib/format";
import { SqlErrorAlert } from "./SqlErrorAlert";

/*
 * DuckDB 런타임 상태(설계 10.2). CSV·엑셀 데이터 소스는 샌드박스에 DuckDB와 excel·encodings 확장이 있어야
 * 준비·질의한다. "런타임 준비"는 202를 받은 뒤 설치가 끝날 때까지 2초마다 상태를 다시 읽는다.
 */
export function RuntimeStatus() {
  const { t } = useTranslation("sql");
  const runtime = useSqlRuntime();
  const install = useInstallSqlRuntime();
  const errorText = useSqlError();

  if (runtime.isPending) return <Spinner className="size-4" label={t("runtime.title")} />;
  if (runtime.isError) {
    return (
      <SqlErrorAlert
        error={runtime.error}
        title={t("runtime.loadFailed")}
        onRetry={() => void runtime.refetch()}
      />
    );
  }
  const status = runtime.data;
  const start = () =>
    install.mutate(undefined, {
      onSuccess: () => toast.success(t("runtime.started")),
      onError: (error) => toast.error(errorText(error)),
    });
  const packages = t("runtime.packages", {
    list: status.packages.join(", "),
    extensions: status.extensions.join(", "),
  });

  if (status.state === "ready") {
    return (
      <p
        role="status"
        aria-label={t("runtime.title")}
        className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-muted"
      >
        <CircleCheck className="size-3.5 text-ok" aria-hidden />
        <span className="font-medium text-ink">{t("runtime.title")}</span>
        <span>{t("runtime.ready", { size: formatBytes(status.bytes) })}</span>
        {status.installed_at ? (
          <span>· {t("runtime.readyAt", { time: formatDateTime(status.installed_at) })}</span>
        ) : null}
        <span className="text-ink-subtle">· {packages}</span>
      </p>
    );
  }
  if (status.state === "installing") {
    return (
      <Alert tone="info" title={t("runtime.title")}>
        <span className="flex items-center gap-2" role="status">
          <Spinner className="size-3.5" />
          {t("runtime.installing")}
        </span>
        <span className="mt-1 block text-xs">{packages}</span>
      </Alert>
    );
  }
  const failed = status.state === "failed";
  return (
    <Alert
      tone={failed ? "danger" : "warn"}
      title={failed ? t("runtime.failed") : t("runtime.title")}
      action={
        <Button size="sm" variant="primary" loading={install.isPending} onClick={start}>
          {failed ? (
            <Download className="size-3.5" aria-hidden />
          ) : (
            <PackageOpen className="size-3.5" aria-hidden />
          )}
          {failed ? t("runtime.retry") : t("runtime.install")}
        </Button>
      }
    >
      {failed && status.error ? <span className="block">{status.error.message}</span> : null}
      <span className="block">{t("runtime.missing")}</span>
      <span className="block">{t("runtime.size")}</span>
      <span className="mt-1 block text-xs">{packages}</span>
    </Alert>
  );
}
