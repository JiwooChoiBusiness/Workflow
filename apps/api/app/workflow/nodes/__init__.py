# pyright: strict
"""노드 레지스트리(설계 2.1, 3.11).

1차 노드가 모두 구현되어 있다: 제어 노드 7종(M11), 모델·HTTP·코드 노드 5종(M13), iteration·agent
(M14). 2차 노드 5종(loop, list_operator, document_extractor, tool, human_input)은 M19에서 더했다.
iteration_start·loop_start는 하위 그래프의 진입 자리라 실행하지 않지만 출력(item·index)을 알리려고
등록한다. 3단계 자리(knowledge_retrieval)는 등록하지 않는다(캔버스 팔레트에 나오지 않는다).
"""

from app.workflow.nodes.agent import AgentRunner
from app.workflow.nodes.aggregator import AggregatorRunner
from app.workflow.nodes.answer import AnswerRunner
from app.workflow.nodes.assigner import AssignerRunner
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.classifier import ClassifierRunner
from app.workflow.nodes.code import CodeRunner
from app.workflow.nodes.document_extractor import DocumentExtractorRunner
from app.workflow.nodes.end import EndRunner
from app.workflow.nodes.extractor import ExtractorRunner
from app.workflow.nodes.http_request import HttpRequestRunner
from app.workflow.nodes.human_input import HumanInputRunner
from app.workflow.nodes.if_else import IfElseRunner
from app.workflow.nodes.iteration import IterationRunner, IterationStartRunner
from app.workflow.nodes.list_operator import ListOperatorRunner
from app.workflow.nodes.llm import LlmRunner
from app.workflow.nodes.loop import LoopRunner, LoopStartRunner
from app.workflow.nodes.start import StartRunner
from app.workflow.nodes.template import TemplateRunner
from app.workflow.nodes.tool import ToolRunner

_DEFAULT = NodeRegistry(
    [
        StartRunner(),
        EndRunner(),
        AnswerRunner(),
        TemplateRunner(),
        IfElseRunner(),
        AggregatorRunner(),
        AssignerRunner(),
        LlmRunner(),
        HttpRequestRunner(),
        CodeRunner(),
        ClassifierRunner(),
        ExtractorRunner(),
        AgentRunner(),
        IterationRunner(),
        IterationStartRunner(),
        LoopRunner(),
        LoopStartRunner(),
        ListOperatorRunner(),
        DocumentExtractorRunner(),
        ToolRunner(),
        HumanInputRunner(),
    ]
)


def default_registry() -> NodeRegistry:
    """기본 레지스트리. 고치지 말고 테스트 등에서 노드를 더하려면 copy()를 쓴다."""
    return _DEFAULT
