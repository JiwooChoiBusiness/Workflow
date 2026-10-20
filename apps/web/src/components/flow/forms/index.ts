import type { ComponentType } from "react";
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
};
