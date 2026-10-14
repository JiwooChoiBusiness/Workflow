import { useTranslation } from "react-i18next";
import { CopyButton } from "@/components/ui/copy-button";

// 키 원문은 만들 때 한 번만 보이므로 평소에는 셸 변수로 둔다.
export const API_KEY_VARIABLE = "$API_KEY";

export type ApiExampleId = "stream" | "blocking" | "conversations" | "task";

export interface ApiExample {
  id: ApiExampleId;
  command: string;
}

// 화면에 그대로 보일 예시. 워크플로우처럼 다른 경로를 쓰는 곳은 이 모양으로 만들어 넘긴다.
export interface LabeledApiExample {
  id: string;
  title: string;
  hint: string;
  command: string;
}

export function serverOrigin(): string {
  return typeof window === "undefined" ? "http://127.0.0.1:8500" : window.location.origin;
}

// 본문은 here-document로 넘긴다. Windows의 curl은 명령줄 인자를 코드 페이지로 바꿔 한글이 깨진다.
export function withBody(lines: string[], payload: unknown): string {
  return [...lines, "  --data-binary @- <<'JSON'", JSON.stringify(payload), "JSON"].join("\n");
}

export function apiExamples(origin: string, key: string = API_KEY_VARIABLE): ApiExample[] {
  const auth = `  -H "Authorization: Bearer ${key}"`;
  const json = '  -H "Content-Type: application/json" \\';
  const chat = (mode: "streaming" | "blocking") =>
    withBody(
      [
        `curl${mode === "streaming" ? " -N" : ""} ${origin}/api/v1/chat-messages \\`,
        `${auth} \\`,
        json,
      ],
      { query: "작업공간에 어떤 파일이 있는지 알려줘", user: "me", response_mode: mode },
    );
  return [
    { id: "stream", command: chat("streaming") },
    { id: "blocking", command: chat("blocking") },
    {
      id: "conversations",
      command: [`curl "${origin}/api/v1/conversations?user=me&limit=20" \\`, auth].join("\n"),
    },
    {
      id: "task",
      command: withBody([`curl ${origin}/api/v1/agent-tasks \\`, `${auth} \\`, json], {
        instruction: "{{a}}와 {{b}}를 더한 값을 sum에 담아 제출한다",
        inputs: { a: 2, b: 3 },
        output_schema: {
          type: "object",
          properties: { sum: { type: "integer" } },
          required: ["sum"],
          additionalProperties: false,
        },
        user: "me",
        response_mode: "blocking",
      }),
    },
  ];
}

function useAgentExamples(apiKey: string | undefined): LabeledApiExample[] {
  const { t } = useTranslation("agents");
  return apiExamples(serverOrigin(), apiKey).map((example) => ({
    id: example.id,
    title: t(`apiExamples.items.${example.id}.title`),
    hint: t(`apiExamples.items.${example.id}.hint`),
    command: example.command,
  }));
}

export function ApiExampleItems({ examples }: { examples: LabeledApiExample[] }) {
  const { t } = useTranslation("agents");
  return (
    <ul aria-label={t("apiExamples.title")} className="flex flex-col gap-4">
      {examples.map((example) => (
        <li key={example.id}>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-sm font-medium text-ink">
              {example.title}
              <span className="ml-2 text-xs font-normal text-ink-muted">{example.hint}</span>
            </span>
            <CopyButton
              value={example.command}
              size="iconSm"
              variant="ghost"
              label={t("apiExamples.copy", { name: example.title })}
            />
          </div>
          {/* 합자 글꼴이 <<를 ≪처럼 그리면 here-document로 읽히지 않는다. */}
          <pre className="overflow-x-auto rounded-md bg-code p-3 font-mono text-xs [font-variant-ligatures:none] text-code-ink">
            {example.command}
          </pre>
        </li>
      ))}
    </ul>
  );
}

export function ApiExampleList({ apiKey }: { apiKey?: string }) {
  return <ApiExampleItems examples={useAgentExamples(apiKey)} />;
}
