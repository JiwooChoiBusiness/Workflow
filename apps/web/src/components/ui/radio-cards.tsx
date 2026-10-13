import { RadioGroup } from "radix-ui";
import { useId, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface RadioCardOption {
  value: string;
  label: ReactNode;
  description?: ReactNode;
}

export function RadioCards({
  value,
  onValueChange,
  options,
  ariaLabel,
  className,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: RadioCardOption[];
  ariaLabel: string;
  className?: string;
}) {
  const baseId = useId();
  return (
    <RadioGroup.Root
      value={value}
      onValueChange={onValueChange}
      aria-label={ariaLabel}
      className={cn("flex flex-col gap-2", className)}
    >
      {options.map((option) => {
        const id = `${baseId}-${option.value}`;
        return (
          <label
            key={option.value}
            htmlFor={id}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 transition-colors",
              value === option.value
                ? "border-accent bg-accent-soft"
                : "border-line bg-surface hover:border-line-strong",
            )}
          >
            <RadioGroup.Item
              id={id}
              value={option.value}
              className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border border-line-strong bg-surface data-[state=checked]:border-accent"
            >
              <RadioGroup.Indicator className="block size-2 rounded-full bg-accent" />
            </RadioGroup.Item>
            <span className="min-w-0">
              <span className="block text-sm font-medium text-ink">{option.label}</span>
              {option.description ? (
                <span className="mt-0.5 block text-xs text-ink-muted">{option.description}</span>
              ) : null}
            </span>
          </label>
        );
      })}
    </RadioGroup.Root>
  );
}
