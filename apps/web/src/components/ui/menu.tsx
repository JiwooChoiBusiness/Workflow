import { DropdownMenu as RadixMenu } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

export const Menu = RadixMenu.Root;
export const MenuTrigger = RadixMenu.Trigger;

export function MenuContent({
  className,
  sideOffset = 4,
  ...props
}: ComponentProps<typeof RadixMenu.Content>) {
  return (
    <RadixMenu.Portal>
      <RadixMenu.Content
        sideOffset={sideOffset}
        className={cn(
          "z-50 min-w-40 rounded-md border border-line bg-surface p-1 shadow-lg",
          className,
        )}
        {...props}
      />
    </RadixMenu.Portal>
  );
}

export function MenuItem({
  className,
  tone,
  ...props
}: ComponentProps<typeof RadixMenu.Item> & { tone?: "danger" }) {
  return (
    <RadixMenu.Item
      className={cn(
        "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none",
        "data-[disabled]:opacity-50 data-[highlighted]:bg-hover",
        tone === "danger" ? "text-danger" : "text-ink",
        className,
      )}
      {...props}
    />
  );
}

export function MenuLabel({ className, ...props }: ComponentProps<typeof RadixMenu.Label>) {
  return (
    <RadixMenu.Label className={cn("px-2 py-1.5 text-xs text-ink-muted", className)} {...props} />
  );
}

export function MenuSeparator({ className, ...props }: ComponentProps<typeof RadixMenu.Separator>) {
  return <RadixMenu.Separator className={cn("my-1 h-px bg-line", className)} {...props} />;
}
