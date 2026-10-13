import { LoaderCircle } from "lucide-react";
import { cn } from "@/lib/cn";

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <LoaderCircle
      className={cn("size-4 animate-spin text-current", className)}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "status" : undefined}
    />
  );
}

export function PageSpinner({ label }: { label: string }) {
  return (
    <div className="flex h-full min-h-40 items-center justify-center gap-2 text-ink-muted">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}
