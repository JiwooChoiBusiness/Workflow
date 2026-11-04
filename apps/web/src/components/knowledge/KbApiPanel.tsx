import { useTranslation } from "react-i18next";
import type { KnowledgeBaseOut } from "@/api/types";
import {
  API_KEY_VARIABLE,
  ApiExampleItems,
  serverOrigin,
  withBody,
  type LabeledApiExample,
} from "@/components/builder/ApiExamples";
import { ApiKeyPanelView } from "@/components/builder/ApiKeyPanel";
import { Panel } from "@/components/ui/layout";
import { useCreateKbApiKey, useKbApiKeys, useRevokeKbApiKey } from "@/features/knowledge/api";

type ExampleId = "retrieve" | "createByText" | "indexingStatus" | "documents";

// 서비스 API datasets 경로의 호출 예시(설계 8.5). 키를 만들기 전에는 셸 변수 $API_KEY로 둔다.
export function useDatasetExamples(kbId: string, apiKey?: string): LabeledApiExample[] {
  const { t } = useTranslation("knowledge");
  const base = `${serverOrigin()}/api/v1/datasets/${kbId}`;
  const auth = `  -H "Authorization: Bearer ${apiKey ?? API_KEY_VARIABLE}"`;
  const json = '  -H "Content-Type: application/json" \\';
  const item = (id: ExampleId, command: string): LabeledApiExample => ({
    id,
    title: t(`api.examples.${id}.title`),
    hint: t(`api.examples.${id}.hint`),
    command,
  });
  return [
    item(
      "retrieve",
      withBody([`curl ${base}/retrieve \\`, `${auth} \\`, json], { query: t("api.sampleQuery") }),
    ),
    item(
      "createByText",
      withBody([`curl ${base}/document/create-by-text \\`, `${auth} \\`, json], {
        name: t("api.sampleName"),
        text: t("api.sampleText"),
      }),
    ),
    item(
      "indexingStatus",
      [`curl "${base}/documents/<batch>/indexing-status" \\`, auth].join("\n"),
    ),
    item("documents", [`curl "${base}/documents?page=1&limit=20" \\`, auth].join("\n")),
  ];
}

function Examples({ kbId, apiKey }: { kbId: string; apiKey?: string }) {
  return <ApiExampleItems examples={useDatasetExamples(kbId, apiKey)} />;
}

// API 탭(설계 8.5, 확장): 이 지식 베이스 하나만 보는 키와 호출 예시. 키 목록과 예시는 에이전트·워크플로우와 같은
// 부품(ApiKeyPanelView, ApiExampleItems)에 KB 대상을 넘긴다. 지식 베이스에는 발행·버전이 없어 발행 패널은 없다.
export function KbApiPanel({ kb }: { kb: KnowledgeBaseOut }) {
  const { t } = useTranslation("knowledge");
  const { t: ta } = useTranslation("agents");
  const keys = useKbApiKeys(kb.id);
  const create = useCreateKbApiKey(kb.id);
  const revoke = useRevokeKbApiKey(kb.id);
  return (
    <div className="flex flex-col gap-4">
      <ApiKeyPanelView
        source={{ keys, create: create.mutateAsync, revoke }}
        description={t("api.keysDescription")}
        renderExamples={(apiKey) => <Examples kbId={kb.id} apiKey={apiKey} />}
      />
      <Panel title={ta("apiExamples.title")} description={t("api.examplesDescription")}>
        <Examples kbId={kb.id} />
      </Panel>
    </div>
  );
}
