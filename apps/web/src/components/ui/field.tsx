import { Label as RadixLabel } from "radix-ui";
import {
  createContext,
  use,
  useId,
  type AriaAttributes,
  type ComponentProps,
  type ReactNode,
} from "react";
import { cn } from "@/lib/cn";

interface FieldContextValue {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
  required: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

// Input·Textarea·Select가 Field 안에 있으면 id와 aria-describedby·invalid·required를 여기서 받는다.
export function useFieldControl(props: {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: AriaAttributes["aria-invalid"];
  "aria-required"?: AriaAttributes["aria-required"];
}) {
  const field = use(FieldContext);
  return {
    id: props.id ?? field?.id,
    "aria-describedby": props["aria-describedby"] ?? field?.describedBy,
    "aria-invalid": props["aria-invalid"] ?? (field?.invalid || undefined),
    "aria-required": props["aria-required"] ?? (field?.required || undefined),
  };
}

export function Label({ className, ...props }: ComponentProps<typeof RadixLabel.Root>) {
  return (
    <RadixLabel.Root
      className={cn("text-sm font-medium text-ink select-none", className)}
      {...props}
    />
  );
}

export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string;
  id?: string;
  required?: boolean;
  className?: string;
  children: ReactNode;
  // 스위치·체크박스처럼 라벨을 컨트롤 옆에 두는 경우
  inline?: boolean;
}

export function Field({
  label,
  hint,
  error,
  id,
  required,
  className,
  children,
  inline,
}: FieldProps) {
  const generated = useId();
  const controlId = id ?? generated;
  const hintId = hint ? `${controlId}-hint` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  const value: FieldContextValue = {
    id: controlId,
    describedBy,
    invalid: Boolean(error),
    required: Boolean(required),
  };
  return (
    <FieldContext value={value}>
      <div className={cn(inline ? "flex items-start gap-3" : "flex flex-col gap-1.5", className)}>
        {inline ? children : null}
        {label ? (
          <div className={cn(inline && "flex flex-col gap-0.5 pt-px")}>
            <Label htmlFor={controlId}>
              {label}
              {/* 필수 여부는 컨트롤의 aria-required로 알리고, 별표는 이름에 섞이지 않게 숨긴다. */}
              {required ? (
                <span className="ml-0.5 text-danger" aria-hidden>
                  *
                </span>
              ) : null}
            </Label>
            {inline && hint ? (
              <p id={hintId} className="text-xs text-ink-muted">
                {hint}
              </p>
            ) : null}
          </div>
        ) : null}
        {inline ? null : children}
        {!inline && hint ? (
          <p id={hintId} className="text-xs text-ink-muted">
            {hint}
          </p>
        ) : null}
        {error ? (
          <p id={errorId} className="text-xs text-danger" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </FieldContext>
  );
}
