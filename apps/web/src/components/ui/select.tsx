import { Check, ChevronDown } from "lucide-react";
import { Select as RadixSelect } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { useFieldControl } from "./field";

export interface SelectOption {
  // Radix Select는 빈 문자열 값을 받지 않는다. "값 없음"은 쓰는 쪽에서 별도 표식 값으로 둔다.
  value: string;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}

export interface SelectProps {
  value: string | undefined;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
  "aria-label"?: string;
  // 닫힌 상태에서 보일 내용. 비우면 고른 항목의 label
  renderValue?: (option: SelectOption | undefined) => ReactNode;
}

export function Select({
  value,
  onValueChange,
  options,
  placeholder,
  disabled,
  className,
  renderValue,
  ...props
}: SelectProps) {
  const field = useFieldControl(props);
  const selected = options.find((option) => option.value === value);
  return (
    <RadixSelect.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <RadixSelect.Trigger
        {...field}
        aria-label={props["aria-label"]}
        className={cn(
          "flex h-8 w-full items-center justify-between gap-2 rounded-md border border-line bg-surface px-2.5",
          "text-left text-sm text-ink transition-colors hover:border-line-strong",
          "focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
          "disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger",
          "data-[placeholder]:text-ink-subtle",
          className,
        )}
      >
        <span className="min-w-0 truncate">
          {renderValue ? (
            renderValue(selected)
          ) : (
            <RadixSelect.Value placeholder={placeholder}>{selected?.label}</RadixSelect.Value>
          )}
        </span>
        <RadixSelect.Icon>
          <ChevronDown className="size-4 text-ink-subtle" aria-hidden />
        </RadixSelect.Icon>
      </RadixSelect.Trigger>
      <RadixSelect.Portal>
        <RadixSelect.Content
          position="popper"
          sideOffset={4}
          className={cn(
            "z-50 max-h-[min(24rem,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)]",
            "overflow-hidden rounded-md border border-line bg-surface shadow-lg",
          )}
        >
          <RadixSelect.Viewport className="p-1">
            {options.map((option) => (
              <RadixSelect.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className={cn(
                  "relative flex cursor-default flex-col rounded-sm py-1.5 pr-2 pl-7 text-sm text-ink outline-none select-none",
                  "data-[disabled]:opacity-50 data-[highlighted]:bg-hover",
                )}
              >
                <RadixSelect.ItemIndicator className="absolute top-2 left-2">
                  <Check className="size-3.5 text-accent" aria-hidden />
                </RadixSelect.ItemIndicator>
                <RadixSelect.ItemText>{option.label}</RadixSelect.ItemText>
                {option.hint ? <span className="text-xs text-ink-muted">{option.hint}</span> : null}
              </RadixSelect.Item>
            ))}
          </RadixSelect.Viewport>
        </RadixSelect.Content>
      </RadixSelect.Portal>
    </RadixSelect.Root>
  );
}
