"""시스템 프롬프트 조립(설계 4.8).

시스템 프롬프트와 도구 목록은 세션 안에서 같아야 Gemini 암묵 캐시의 접두가 유지된다. 그래서 여기에는
세션 동안 바뀌지 않는 정보만 넣는다. 현재 시각처럼 변하는 정보는 각 런의 사용자 메시지 뒤의
<context> 블록으로 간다(context.py). 승인 정책 문구도 세션 자동 승인이 아니라 에이전트 버전의 모드로
쓴다.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from functools import cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel

from app.agent.child_runs import SubAgentChoice
from app.agent.tools.call_agent import available_agents_block
from app.llm.types import ToolSpec
from app.schemas.common import ApprovalMode

ShellName = Literal["powershell", "pwsh", "bash"]

_SHELL_DESCRIPTIONS: dict[ShellName, str] = {
    "powershell": "Windows PowerShell 5.1(`powershell.exe`)이다. 명령은 PowerShell 문법으로 쓴다.",
    "pwsh": "PowerShell 7(`pwsh`)이다. 명령은 PowerShell 문법으로 쓴다.",
    "bash": "bash다. 명령은 bash 문법으로 쓴다.",
}
_ENCODING_HINTS: dict[ShellName, str] = {
    "powershell": (
        "PowerShell 5.1에서 파일을 쓸 때는 `-Encoding utf8`을 붙인다"
        "(`>` 리다이렉션은 UTF-16 파일을 만든다). 이렇게 쓴 파일은 BOM이 있으므로 "
        "파이썬에서 읽을 때 `encoding='utf-8-sig'`를 쓴다."
    ),
    "pwsh": (
        "파일은 UTF-8로 쓰고, 파이썬에서 읽을 때 BOM이 있을 수 있으면 "
        "`encoding='utf-8-sig'`를 쓴다."
    ),
    "bash": "파일은 UTF-8로 쓴다.",
}
_APPROVAL_POLICIES: dict[ApprovalMode, str] = {
    "all_shell": (
        "`shell`, `run_python`, `install_packages`와 GET·HEAD가 아닌 `http_request`는 "
        "실행 직전에 사용자가 명령 전문을 보고 승인한다. 승인을 기다리는 동안 작업이 멈춘다. "
        "나머지 도구는 바로 실행된다."
    ),
    "http_writes": (
        "GET·HEAD가 아닌 `http_request`는 실행 직전에 사용자가 승인한다. "
        "나머지 도구는 바로 실행된다."
    ),
    "off": "모든 도구가 승인 없이 바로 실행된다. 그만큼 신중하게 쓴다.",
}
_SKILLS_GUIDE = (
    "작업이 아래 스킬의 설명에 해당하면 먼저 그 스킬의 SKILL.md를 read_file로 읽고 지시를 따른다. "
    "스킬의 스크립트는 run_python이나 셸로 실행한다."
)
# 하위 에이전트를 골라 두었지만 깊이·호출 수 한도에 닿아 call_agent가 없는 런(오케스트레이션 3.9).
_SUB_AGENTS_UNAVAILABLE = (
    "# 하위 에이전트\n\n"
    "이 런은 하위 에이전트를 부를 수 없는 깊이(또는 호출 수 한도)에 있다. 맡기려던 일도 직접 한다."
)
_CHILD_TASK_LINE = (
    "- 이 과업은 상위 에이전트 {parent}이(가) 맡긴 것이다. 결과는 상위 에이전트가 읽는다. "
    "사용자에게 직접 물을 수 없으므로 확인이 필요한 것은 짐작으로 진행하지 말고 결과 글의 "
    "'확인이 필요한 것' 항목에 적어 돌려준다."
)


class SkillPrompt(BaseModel):
    name: str
    description: str
    # 작업공간 상대 경로(skills/<slug>/SKILL.md).
    path: str


class WorkspacePromptInfo(BaseModel):
    root: str
    shell: ShellName
    platform: str
    approval_mode: ApprovalMode


@cache
def load_prompt(name: str) -> str:
    return resources.files("app.agent").joinpath("prompts", name).read_text(encoding="utf-8")


def _fill(template: str, values: dict[str, str]) -> str:
    # PowerShell 예시의 $·{}와 섞이지 않게 {{이름}} 자리만 바꾼다.
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


class PromptBuilder:
    def build(
        self,
        *,
        instructions: str,
        skills: Sequence[SkillPrompt],
        workspace: WorkspacePromptInfo,
        task_block: str | None = None,
        sub_agents: Sequence[SubAgentChoice] | None = None,
        sub_agents_unavailable: bool = False,
    ) -> str:
        """서문 → 에이전트 지시문 → 스킬 목록 → 과업 지시(M8) → 하위 에이전트 순서.

        sub_agents는 call_agent가 붙는 런에만 준다. 골라 둔 하위 에이전트가 있는데 깊이·호출 수
        한도로 도구를 붙이지 않는 런은 sub_agents_unavailable로 한 줄만 남긴다.
        """
        preamble = _fill(
            load_prompt("system_preamble.md"),
            {
                "platform": workspace.platform,
                "workspace_root": workspace.root,
                "shell_description": _SHELL_DESCRIPTIONS[workspace.shell],
                "encoding_hint": _ENCODING_HINTS[workspace.shell],
                "approval_policy": _APPROVAL_POLICIES[workspace.approval_mode],
            },
        )
        parts = [preamble.strip()]
        if instructions.strip():
            parts.append("# 에이전트 지시문\n\n" + instructions.strip())
        if skills:
            lines = ["# 스킬", "", _SKILLS_GUIDE, "", "<available_skills>"]
            for skill in skills:
                lines.append(f"- name: {skill.name}")
                lines.append(f"  description: {skill.description}")
                lines.append(f"  path: {skill.path}")
            lines.append("</available_skills>")
            parts.append("\n".join(lines))
        if task_block:
            parts.append(task_block.strip())
        if sub_agents:
            parts.append(
                _fill(
                    load_prompt("sub_agents.md"),
                    {"available_agents": available_agents_block(sub_agents)},
                ).strip()
            )
        elif sub_agents_unavailable:
            parts.append(_SUB_AGENTS_UNAVAILABLE)
        return "\n\n".join(parts) + "\n"


def task_block(
    output_schema: Mapping[str, Any] | None,
    *,
    submit_tool: str,
    parent_agent: str | None = None,
) -> str:
    """과업 모드(4.10)의 지시.

    출력 스키마는 도구 입력 스키마로도 보내지만 모델에 보내는 쪽은 일부 키를 빼므로(model_schema)
    전체를 여기에도 적는다. parent_agent는 call_agent로 맡겨진 과업일 때 상위 에이전트 이름이다.
    """
    lines = [
        "# 과업",
        "",
        "이 세션은 외부 프로그램이 맡긴 과업 하나를 처리한다. "
        "사용자 메시지의 지시를 끝까지 수행한다.",
        "- 입력 값 전체는 `task/inputs.json`, 입력 파일은 `task/files/`에 있다. "
        "이 폴더는 고치지 않는다.",
        "- 결과 파일은 `outputs/`에 둔다.",
    ]
    if parent_agent is not None:
        lines.append(_CHILD_TASK_LINE.format(parent=parent_agent or "(이름 없음)"))
    if output_schema is None:
        lines.append("- 작업을 마치면 결과를 마지막 답변 하나로 정리한다.")
        return "\n".join(lines)
    lines.append(
        f"- 작업을 모두 마치면 반드시 `{submit_tool}` 도구를 한 번 호출해 결과를 제출한다. 결과는 "
        "아래 출력 스키마를 따른다. 제출이 받아들여지면 과업이 끝난다."
    )
    schema_text = json.dumps(output_schema, ensure_ascii=False, indent=2, sort_keys=True)
    lines.extend(["", "<output_schema>", schema_text, "</output_schema>"])
    return "\n".join(lines)


def prompt_hash(system: str, tools: Sequence[ToolSpec]) -> str:
    """session.prompt_hash. 시스템 프롬프트와 모델에 보내는 도구 목록이 같으면 같은 값이다."""
    tools_json = json.dumps(
        [tool.model_dump(mode="json") for tool in tools],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{system}\0{tools_json}".encode()).hexdigest()
