# pyright: strict
"""knowledge_retrieval 노드(3단계 설계 8.1).

고른 지식 베이스(1~5개)를 질의 변수로 검색한다. 검색은 KnowledgePort(서비스의
KnowledgeSearchService.retrieve)가 하고, 노드는 결과를 Dify 노드 출력과 같은 모양(result)과
프롬프트에 그대로 넣을 글(result_text)로 바꾼다. 결과가 0개면 빈 배열이다(오류가 아니다).

인용 목록(retriever_resources)은 이 노드가 아니라 이 결과를 context로 받은 llm 노드가 낸다(8.3).
그래서 result 항목의 metadata에 인용을 다시 만들 수 있는 필드를 모두 둔다.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.knowledge.retrieval.filters import (
    FieldInfo,
    FilterError,
    check_fields,
    clause,
    common_fields,
)
from app.knowledge.retrieval.rerank import ModelCall
from app.llm.pricing import cost_usd
from app.schemas.citations import RetrieverResourceData, document_block
from app.schemas.events import UsageTotal
from app.schemas.knowledge import FieldName, RetrievalSettings
from app.schemas.retrieval import (
    MAX_CONDITIONS,
    ComparisonOperator,
    ConditionScalar,
    FilterMode,
    FilterModel,
    LogicalOperator,
    MetadataCondition,
    MetadataFilter,
    canonical_operator,
)
from app.schemas.workflow import Issue, IssueLevel
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    KnowledgeBaseRef,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
)
from app.workflow.ports import KnowledgePortError

MAX_KBS = 5
# 자동 필터(10초)와 LLM 재순위(20초)가 들어가는 시간이다(8.1).
RETRIEVAL_TIMEOUT_SEC = 30
_NO_FIELD_ID = uuid.UUID(int=0)


class ConditionVar(BaseModel):
    """조건 값을 변수에서 읽는다. 실행할 때 그 값으로 바꾼다."""

    model_config = ConfigDict(extra="forbid")

    var: str


def _value_field(index: int) -> str:
    # 화면이 조건 값 칸에 이슈를 붙이는 경로다.
    return f"metadata_filter.conditions.{index}.value"


class NodeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName
    comparison_operator: ComparisonOperator
    # 상수(글·수·목록) 또는 {var}. empty·not empty는 값을 보지 않는다. {var} 모양이 틀린 객체도
    # 설정으로는 받고, 검증이 그 칸(metadata_filter.conditions.N.value)에 하나의 이슈로 알린다.
    value: ConditionVar | ConditionScalar | list[ConditionScalar] | dict[str, Any] | None = None

    @field_validator("comparison_operator", mode="before")
    @classmethod
    def _alias(cls, value: object) -> object:
        return canonical_operator(value) if isinstance(value, str) else value


class NodeMetadataFilter(BaseModel):
    """메타데이터 필터(7.5). manual은 conditions, automatic은 모델이 질의에서 조건을 만든다."""

    model_config = ConfigDict(extra="forbid")

    mode: FilterMode = "disabled"
    logical_operator: LogicalOperator = "and"
    conditions: list[NodeCondition] = Field(
        default_factory=list[NodeCondition], max_length=MAX_CONDITIONS
    )
    model: FilterModel = Field(default_factory=FilterModel)

    @field_validator("logical_operator", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class KnowledgeRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID], max_length=MAX_KBS)
    # string 변수. 워크플로우에는 sys.query가 없으므로 고른다.
    query_var: str = "sys.query"
    # 비우면 KB가 하나일 때 그 KB의 검색 설정이고, 여럿이면 필수다.
    retrieval: RetrievalSettings | None = None
    metadata_filter: NodeMetadataFilter = Field(default_factory=NodeMetadataFilter)


def _issue(level: IssueLevel, code: str, message: str, field: str) -> Issue:
    return Issue(level=level, code=code, message=message, field=field)


def result_item(resource: RetrieverResourceData) -> dict[str, Any]:
    """result 항목. Dify 노드 출력처럼 content·title·metadata이고 metadata는 인용 필드다."""
    data = resource.model_dump(mode="json")
    content = data.pop("content")
    metadata = {
        key: value
        for key, value in data.items()
        if value is not None or key not in ("answer", "child_chunks", "fusion", "fusion_score")
    }
    return {"content": content, "title": resource.document_name, "metadata": metadata}


def resource_of(item: object, position: int) -> RetrieverResourceData | None:
    """result 항목을 인용으로 되돌린다. 다른 노드가 만든 배열이라 모양이 다르면 None이다."""
    if not isinstance(item, dict):
        return None
    found = cast(dict[str, Any], item)
    raw = found.get("metadata")
    if not isinstance(raw, dict):
        return None
    metadata = cast(dict[str, Any], raw)
    try:
        return RetrieverResourceData.model_validate(
            {**metadata, "content": found.get("content"), "position": position}
        )
    except ValidationError:
        return None


def passage(item: RetrieverResourceData) -> str:
    return f"{item.content}\n{item.answer}" if item.answer else item.content


def result_text(resources: Sequence[RetrieverResourceData]) -> str:
    """자료마다 <document index="n" name="문서" source="KB">글</document>로 감싸 잇는다(8.1, 14.1).

    번호는 이 검색 결과 안의 순서다. 프롬프트에 그대로 넣는 글이라 인용은 남지 않는다(인용은
    llm의 context).
    """
    return "\n".join(
        document_block(
            passage(item), index=item.position, name=item.document_name, source=item.dataset_name
        )
        for item in resources
    )


async def report_calls(ctx: NodeContext, calls: Sequence[ModelCall]) -> UsageTotal | None:
    """자동 필터·재순위의 채팅 호출마다 node.usage를 내고 합을 돌려준다(8.1)."""
    total: UsageTotal | None = None
    prices = ctx.ports.node_settings.model_prices
    for call in calls:
        usage = call.usage
        cost = cost_usd(call.model, usage, prices)
        await ctx.emit(
            "node.usage",
            {
                "model": call.model,
                "key_index": call.key_index,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "estimated": usage.estimated,
                "cost_usd": cost,
            },
        )
        part = UsageTotal(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens or 0,
            cached_input_tokens=usage.cached_input_tokens or 0,
            cost_usd=cost,
            estimated=usage.estimated,
        )
        if total is None:
            total = part
            continue
        total.input_tokens += part.input_tokens
        total.output_tokens += part.output_tokens
        total.reasoning_tokens += part.reasoning_tokens
        total.cached_input_tokens += part.cached_input_tokens
        total.estimated = total.estimated or part.estimated
        if part.cost_usd is not None:
            total.cost_usd = (total.cost_usd or 0.0) + part.cost_usd
    return total


def _scalar(value: object) -> object:
    # 변수 값의 참·거짓은 메타데이터 값(글)과 비교할 수 있게 글로 바꾼다.
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class KnowledgeRetrievalRunner(NodeRunner[KnowledgeRetrievalConfig]):
    type = "knowledge_retrieval"
    Config = KnowledgeRetrievalConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (
        OutputSpec("result", "array[object]"),
        OutputSpec("result_text", "string"),
        OutputSpec("count", "number"),
    )

    def var_refs(self, config: KnowledgeRetrievalConfig) -> list[VarRef]:
        refs = [VarRef("query_var", config.query_var, "string")] if config.query_var else []
        if config.metadata_filter.mode == "manual":
            for index, condition in enumerate(config.metadata_filter.conditions):
                if isinstance(condition.value, ConditionVar):
                    refs.append(VarRef(_value_field(index), condition.value.var))
        return refs

    def validate(self, config: KnowledgeRetrievalConfig, graph: GraphView) -> list[Issue]:
        issues: list[Issue] = []
        if not config.kb_ids:
            issues.append(
                _issue("error", "kb_required", "검색할 지식 베이스를 하나 이상 고른다", "kb_ids")
            )
        if not config.query_var:
            issues.append(_issue("error", "query_required", "질의 변수를 고른다", "query_var"))
        if config.metadata_filter.mode == "manual":
            for index, condition in enumerate(config.metadata_filter.conditions):
                if isinstance(condition.value, dict):
                    issues.append(
                        _issue(
                            "error",
                            "invalid_selector",
                            '조건 값을 변수에서 읽으려면 {"var": "노드.출력"} 모양으로 쓴다',
                            _value_field(index),
                        )
                    )
        if len(set(config.kb_ids)) > 1 and config.retrieval is None:
            issues.append(
                _issue(
                    "error",
                    "retrieval_settings_required",
                    "지식 베이스를 여럿 고르면 검색 설정(top_k·결합 방식)을 정한다",
                    "retrieval",
                )
            )
        top_k_max = graph.kb_top_k_max()
        retrieval = config.retrieval
        if retrieval is not None and top_k_max is not None and retrieval.top_k > top_k_max:
            issues.append(
                _issue(
                    "error",
                    "top_k_too_large",
                    f"top_k는 {top_k_max} 이하여야 한다(KB_TOP_K_MAX)",
                    "retrieval.top_k",
                )
            )
        known = graph.knowledge_bases()
        if known is None:
            return issues
        refs: dict[uuid.UUID, KnowledgeBaseRef] = {}
        for index, kb_id in enumerate(config.kb_ids):
            ref = known.get(kb_id)
            if ref is None:
                issues.append(
                    _issue(
                        "error",
                        "knowledge_base_not_found",
                        "지식 베이스를 찾을 수 없다. 지워졌거나 다른 PC에서 가져온 노드다",
                        f"kb_ids[{index}]",
                    )
                )
                continue
            refs[kb_id] = ref
            if ref.paid:
                issues.append(paid_issue(ref, f"kb_ids[{index}]"))
        if refs and len(refs) == len(set(config.kb_ids)):
            issues.extend(self._filter_issues(config.metadata_filter, refs))
        return issues

    def _filter_issues(
        self, metadata_filter: NodeMetadataFilter, refs: Mapping[uuid.UUID, KnowledgeBaseRef]
    ) -> list[Issue]:
        per_kb = {
            kb_id: [
                FieldInfo(id=_NO_FIELD_ID, name=name, type=kind)
                for name, kind in ref.fields.items()
            ]
            for kb_id, ref in refs.items()
        }
        if metadata_filter.mode == "automatic":
            if not common_fields(list(per_kb.values())):
                return [
                    _issue(
                        "warning",
                        "metadata_field_mismatch",
                        "고른 지식 베이스에 같은 이름·타입의 메타데이터 필드가 없어 자동 필터가 "
                        "조건을 만들 수 없다",
                        "metadata_filter.mode",
                    )
                ]
            return []
        if metadata_filter.mode != "manual":
            return []
        issues: list[Issue] = []
        for index, condition in enumerate(metadata_filter.conditions):
            where = f"metadata_filter.conditions[{index}]"
            value = condition.value
            variable = isinstance(value, (ConditionVar, dict))
            constant = None if isinstance(value, (ConditionVar, dict)) else value
            plain = MetadataCondition(
                name=condition.name,
                comparison_operator=condition.comparison_operator,
                value=constant,
            )
            try:
                types = check_fields([plain], per_kb)
                if not variable:
                    clause(plain, types[condition.name], index)
            except FilterError as exc:
                field = f"{where}.name" if exc.code != "metadata_condition_invalid" else where
                issues.append(_issue("error", exc.code, exc.message, field))
        return issues

    def timeout_sec(self, config: KnowledgeRetrievalConfig) -> float | None:
        return RETRIEVAL_TIMEOUT_SEC

    async def run(
        self, ctx: NodeContext, config: KnowledgeRetrievalConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        port = ctx.ports.knowledge
        if port is None:
            raise NodeFailure("knowledge_unavailable", "지식 베이스를 쓸 수 없는 실행 환경이다")
        if not config.kb_ids:
            raise NodeFailure("kb_required", "검색할 지식 베이스가 없다")
        value = ctx.resolve(config.query_var, "string") if config.query_var else None
        query = "" if value is None else str(value)
        metadata_filter = self._metadata_filter(ctx, config.metadata_filter)
        try:
            hits = await port.retrieve(
                config.kb_ids,
                query,
                config.retrieval,
                metadata_filter,
                tier=ctx.ports.node_settings.tier,
            )
        except KnowledgePortError as exc:
            raise NodeFailure(
                exc.code, exc.message, retryable=exc.retryable, retry_after=exc.retry_after
            ) from None
        usage = await report_calls(ctx, hits.calls)
        warnings = [
            f"{item.code}: {item.message}"
            + (f" (retry_after={item.retry_after:g}초)" if item.retry_after is not None else "")
            for item in hits.warnings
        ]
        if not hits.query:
            warnings.append("empty_query: 질의가 비어 검색하지 않았다")
        inputs: dict[str, Any] = {
            "query": hits.query,
            "kb_ids": [str(kb_id) for kb_id in config.kb_ids],
        }
        if metadata_filter is not None:
            inputs["metadata_filter"] = metadata_filter.model_dump(mode="json")
        process: dict[str, Any] = {
            "settings": hits.settings.model_dump(mode="json"),
            "latency_ms": hits.latency_ms,
            "calls": [
                {
                    "purpose": call.purpose,
                    "model": call.model,
                    "key_index": call.key_index,
                    "input_tokens": call.usage.input_tokens,
                    "output_tokens": call.usage.output_tokens,
                }
                for call in hits.calls
            ],
        }
        if hits.auto_filter is not None:
            process["auto_filter"] = hits.auto_filter.model_dump(
                mode="json", include={"logical_operator", "conditions"}
            )
        items = [result_item(resource) for resource in hits.resources]
        return NodeResult(
            status="succeeded",
            outputs={
                "result": items,
                "result_text": result_text(hits.resources),
                "count": len(items),
            },
            usage=usage,
            inputs=inputs,
            process=process,
            warnings=warnings,
        )

    def _metadata_filter(
        self, ctx: NodeContext, metadata_filter: NodeMetadataFilter
    ) -> MetadataFilter | None:
        if metadata_filter.mode == "disabled":
            return None
        conditions: list[MetadataCondition] = []
        if metadata_filter.mode == "manual":
            for condition in metadata_filter.conditions:
                value: object = condition.value
                if isinstance(value, ConditionVar):
                    resolved = ctx.resolve(value.var)
                    if isinstance(resolved, list):
                        value = [_scalar(item) for item in cast(list[Any], resolved)]
                    else:
                        value = _scalar(resolved)
                try:
                    conditions.append(
                        MetadataCondition.model_validate(
                            {
                                "name": condition.name,
                                "comparison_operator": condition.comparison_operator,
                                "value": value,
                            }
                        )
                    )
                except ValidationError:
                    raise NodeFailure(
                        "metadata_condition_invalid",
                        f"메타데이터 조건 {condition.name}의 값은 글·수나 그 목록이어야 한다",
                    ) from None
        return MetadataFilter(
            mode=metadata_filter.mode,
            logical_operator=metadata_filter.logical_operator,
            conditions=conditions,
            model=metadata_filter.model,
        )


def paid_issue(ref: KnowledgeBaseRef, field: str) -> Issue:
    return _issue(
        "warning",
        "paid_key_required",
        f"지식 베이스 {ref.name}은(는) 유료 키 전용이다. GEMINI_PAID_API_KEY가 없으면 실행·발행할 "
        "수 없고, 이 워크플로우의 모델 호출은 모두 유료 키로 간다",
        field,
    )
