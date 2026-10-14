import type { UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { ModelPicker, type ModelFields } from "@/components/builder/ModelPicker";
import { useModels } from "@/features/settings/api";
import { Section } from "./shared";

// llm·분류·추출 노드의 모델 설정. 1단계 빌더의 ModelPicker를 그대로 쓰고 "목록에 없어도 저장"만 뺀다.
export function ModelSection<F extends ModelFields>({ form }: { form: UseFormReturn<F> }) {
  const { t } = useTranslation("agents");
  const models = useModels();
  return (
    <Section title={t("settings.model")}>
      <ModelPicker
        form={form}
        models={models.data}
        modelsError={models.error}
        onRetry={() => void models.refetch()}
        unlistedOption={false}
      />
    </Section>
  );
}
