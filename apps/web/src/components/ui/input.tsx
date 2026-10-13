import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";
import { useFieldControl } from "./field";

const control =
  "w-full rounded-md border border-line bg-surface text-sm text-ink placeholder:text-ink-subtle " +
  "transition-colors hover:border-line-strong focus-visible:border-accent focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-60 " +
  "aria-invalid:border-danger";

export function Input({ className, ...props }: ComponentProps<"input">) {
  const field = useFieldControl(props);
  return <input className={cn(control, "h-8 px-2.5", className)} {...props} {...field} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  const field = useFieldControl(props);
  return (
    <textarea className={cn(control, "min-h-20 px-2.5 py-2", className)} {...props} {...field} />
  );
}
