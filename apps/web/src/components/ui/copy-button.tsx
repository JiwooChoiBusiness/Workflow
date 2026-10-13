import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, type ButtonProps } from "./button";

export function CopyButton({
  value,
  label,
  size = "sm",
  variant = "secondary",
}: {
  value: string;
  label?: string;
  size?: ButtonProps["size"];
  variant?: ButtonProps["variant"];
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);
  const iconOnly = size === "icon" || size === "iconSm";
  const text = copied ? t("actions.copied") : (label ?? t("actions.copy"));
  return (
    <Button
      size={size}
      variant={variant}
      aria-label={iconOnly ? text : undefined}
      title={iconOnly ? text : undefined}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => setCopied(true));
      }}
    >
      {copied ? (
        <Check className="size-3.5" aria-hidden />
      ) : (
        <Copy className="size-3.5" aria-hidden />
      )}
      {iconOnly ? null : text}
    </Button>
  );
}
