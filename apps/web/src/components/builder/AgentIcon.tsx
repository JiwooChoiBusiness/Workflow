import { cn } from "@/lib/cn";

export function AgentIcon({
  icon,
  name,
  className,
}: {
  icon: string | null | undefined;
  name: string;
  className?: string;
}) {
  const text = icon?.trim() || name.trim().slice(0, 1).toUpperCase() || "?";
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-10 shrink-0 place-items-center rounded-md bg-accent-soft text-lg font-semibold text-accent",
        className,
      )}
    >
      {text}
    </span>
  );
}
