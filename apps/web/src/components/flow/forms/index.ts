import { lazy, type ComponentType } from "react";
import { AgentForm } from "./AgentForm";
import { AggregatorForm } from "./AggregatorForm";
import { AnswerForm } from "./AnswerForm";
import { AssignerForm } from "./AssignerForm";
import { ClassifierForm } from "./ClassifierForm";
import { CodeForm } from "./CodeForm";
import { DocumentExtractorForm } from "./DocumentExtractorForm";
import { EndForm } from "./EndForm";
import { ExtractorForm } from "./ExtractorForm";
import { HttpForm } from "./HttpForm";
import { HumanInputForm } from "./HumanInputForm";
import { IfElseForm } from "./IfElseForm";
import { IterationForm } from "./IterationForm";
import { ListOperatorForm } from "./ListOperatorForm";
import { LlmForm } from "./LlmForm";
import { LoopForm } from "./LoopForm";
import type { NodeFormProps } from "./shared";
import { StartForm } from "./StartForm";
import { TemplateForm } from "./TemplateForm";
import { ToolForm } from "./ToolForm";

// 지식 노드 폼은 지식 화면 문구(knowledge)와 검색 설정·청크 규칙 폼을 함께 쓴다. 그 노드를 고를 때 따로 받아
// 편집기 묶음을 키우지 않는다.
const KnowledgeRetrievalForm = lazy(async () => ({
  default: (await import("./KnowledgeRetrievalForm")).KnowledgeRetrievalForm,
}));
const KnowledgeIndexForm = lazy(async () => ({
  default: (await import("./KnowledgeIndexForm")).KnowledgeIndexForm,
}));
// SQL 노드 폼도 데이터 소스 문구(sql)를 함께 쓴다. 지식 노드 폼처럼 고를 때 받는다.
const SqlSchemaForm = lazy(async () => ({
  default: (await import("./SqlSchemaForm")).SqlSchemaForm,
}));
const SqlQueryForm = lazy(async () => ({
  default: (await import("./SqlQueryForm")).SqlQueryForm,
}));

// 타입별 속성 폼(설계 7.3). 여기 없는 타입은 속성 패널이 config JSON 편집기로 보인다.
export const NODE_FORMS: Record<string, ComponentType<NodeFormProps>> = {
  start: StartForm,
  end: EndForm,
  answer: AnswerForm,
  template: TemplateForm,
  if_else: IfElseForm,
  variable_aggregator: AggregatorForm,
  variable_assigner: AssignerForm,
  llm: LlmForm,
  http_request: HttpForm,
  code: CodeForm,
  question_classifier: ClassifierForm,
  parameter_extractor: ExtractorForm,
  iteration: IterationForm,
  agent: AgentForm,
  loop: LoopForm,
  list_operator: ListOperatorForm,
  document_extractor: DocumentExtractorForm,
  tool: ToolForm,
  human_input: HumanInputForm,
  knowledge_retrieval: KnowledgeRetrievalForm,
  knowledge_index: KnowledgeIndexForm,
  sql_schema: SqlSchemaForm,
  sql_query: SqlQueryForm,
};
