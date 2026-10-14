import type { TFunction } from "i18next";
import { isKnownType } from "@/features/workflows/nodeTypes";

type TW = TFunction<"workflows">;

export function nodeLabel(t: TW, type: string): string {
  return isKnownType(type) ? t(`nodes.${type}.label`) : type;
}

export function nodeDescription(t: TW, type: string): string {
  return isKnownType(type) ? t(`nodes.${type}.description`) : "";
}
