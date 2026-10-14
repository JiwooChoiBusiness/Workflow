"""call_agent 도구의 스키마·설명·입력 처리(오케스트레이션 2.1)."""

import uuid

from app.agent.child_runs import ChildCall, ChildStartError, SubAgentChoice
from app.agent.tools.base import model_schema, validate_tool_input
from app.agent.tools.call_agent import CallAgentTool, agent_line
from app.schemas.agent_version import ResultSpec


def choice(slug: str, **fields: object) -> SubAgentChoice:
    values: dict[str, object] = {
        "agent_id": uuid.uuid4(),
        "slug": slug,
        "name": f"{slug} 이름",
        "description": f"{slug} 설명",
    }
    values.update(fields)
    return SubAgentChoice.model_validate(values)


def test_schema_lists_the_agents_as_an_enum() -> None:
    tool = CallAgentTool([choice("researcher"), choice("writer")])
    schema = tool.schema

    assert schema["required"] == ["agent", "task"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["agent"]["enum"] == ["researcher", "writer"]
    assert schema["properties"]["files"]["maxItems"] == 20
    # 모델이 스키마를 정하는 json 항목이 없으면 output_schema 인자도 없다.
    assert "output_schema" not in schema["properties"]
    assert tool.pause_kind == "agent_call"
    assert tool.group == "system"


def test_output_schema_argument_only_for_agents_that_take_it() -> None:
    fixed = choice("fixed", result=ResultSpec.model_validate({"format": "json", "schema": {}}))
    free = choice("free", result=ResultSpec(format="json"))
    tool = CallAgentTool([fixed, free, choice("plain")])

    prop = tool.schema["properties"]["output_schema"]
    assert prop["type"] == "object"
    assert "free" in prop["description"] and "fixed" not in prop["description"]
    # 모델에 보내는 스키마에는 properties 없는 object가 그대로 간다(라이브 확인 대상).
    sent = model_schema(tool.schema)
    assert sent["properties"]["output_schema"] == {
        "type": "object",
        "description": prop["description"],
    }
    assert "minLength" not in sent["properties"]["task"]


def test_description_carries_the_available_agents() -> None:
    tool = CallAgentTool(
        [
            choice("researcher", name="조사 담당", description="근거를 모은다"),
            choice("writer", name="작성", description="", result=ResultSpec(format="json")),
        ]
    )
    spec = tool.spec()

    assert spec.name == "call_agent"
    assert "<available_agents>" in spec.description
    assert "- researcher: 조사 담당 — 근거를 모은다 (결과: 글)" in spec.description
    assert "- writer: 작성 (결과: JSON, 호출할 때 output_schema로 스키마를 준다)" in (
        spec.description
    )
    assert (
        agent_line(choice("a", name="A", description="줄\n바꿈")) == "- a: A — 줄 바꿈 (결과: 글)"
    )


def test_input_is_checked_against_this_instance_enum() -> None:
    first = CallAgentTool([choice("researcher")])
    second = CallAgentTool([choice("writer")])

    assert validate_tool_input(first, {"agent": "researcher", "task": "t"}) is None
    # 이름이 같은 도구라도 검증기를 이름으로 캐시하지 않는다.
    assert validate_tool_input(second, {"agent": "writer", "task": "t"}) is None
    error = validate_tool_input(second, {"agent": "researcher", "task": "t"})
    assert error is not None and error.startswith("INVALID_INPUT") and "writer" in error
    assert validate_tool_input(first, {"agent": "researcher", "task": ""}) is not None
    assert validate_tool_input(first, {"agent": "researcher", "task": "t", "x": 1}) is not None


def test_child_call_maps_the_input() -> None:
    agent = choice("researcher")
    tool = CallAgentTool([agent])
    message_id = uuid.uuid4()

    call = tool.child_call(
        "call_1",
        message_id,
        {"agent": "researcher", "task": "조사", "files": ["a.txt", "a.txt", "b.txt"]},
    )

    assert isinstance(call, ChildCall)
    assert call.agent == agent
    assert call.files == ["a.txt", "b.txt"]
    assert call.message_id == message_id and call.tool_call_id == "call_1"
    missing = tool.child_call("call_2", message_id, {"agent": "nobody", "task": "x"})
    assert isinstance(missing, ChildStartError) and missing.code == "agent_not_allowed"
