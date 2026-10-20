/*
 * 8501 시험 서버(LLM_FAKE=true, 2026-10-15, M19)의 콘솔 API로 받은 2차 노드 런. start → tool(json_parse) →
 * list_operator → tool(current_time) → loop(반복 변수 n·log, n ≥ 3이면 멈춤, 최대 5) → human_input → end를
 * 돌려 폼에서 멈춘 런 상세, 잘못된 폼 응답(422)·다른 종류의 응답(409), 폼 응답(202), 재개 이벤트, 끝난 런
 * 상세와 끝난 뒤의 응답(409)을 담았다.
 */
import type { WfEvent } from "@/api/run-events";
import type { WfGraphOut, WfNodeRunOut, WfRunDetail, WfRunOut } from "@/api/types";
import secondNodes from "./fixtures/contract/second-nodes.json";

interface ErrorReply {
  status: number;
  body: { error: { code: string; message: string; details?: unknown } };
}

export interface SecondNodesContract {
  workflowId: string;
  runId: string;
  graph: WfGraphOut;
  events: WfEvent[];
  waiting: WfRunDetail;
  // GET /wf-runs/{rid}/node-runs?iteration=loop_1
  loopChildren: { items: WfNodeRunOut[] };
  invalid: ErrorReply;
  wrongKind: ErrorReply;
  input: WfRunOut;
  resume: WfEvent[];
  done: WfRunDetail;
  again: ErrorReply;
}

export const secondNodesContract = secondNodes as unknown as SecondNodesContract;
