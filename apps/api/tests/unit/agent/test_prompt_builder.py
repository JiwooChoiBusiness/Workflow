from app.agent.prompt_builder import (
    PromptBuilder,
    SkillPrompt,
    WorkspacePromptInfo,
    load_prompt,
    prompt_hash,
)
from app.agent.tools import tools_for_groups

WORKSPACE = WorkspacePromptInfo.model_validate(
    {
        "root": r"C:\wf-data\workspaces\abc",
        "shell": "powershell",
        "platform": "Windows 11",
        "approval_mode": "all_shell",
    }
)


def build(**overrides: object) -> str:
    values: dict[str, object] = {"instructions": "", "skills": [], "workspace": WORKSPACE}
    values.update(overrides)
    return PromptBuilder().build(**values)  # pyright: ignore[reportArgumentType]


def test_preamble_has_workspace_rules_and_no_placeholders() -> None:
    prompt = build()

    assert "{{" not in prompt and "}}" not in prompt
    assert r"`C:\wf-data\workspaces\abc`" in prompt
    assert "Windows 11" in prompt
    assert "Windows PowerShell 5.1" in prompt
    assert "`-Encoding utf8`" in prompt and "utf-8-sig" in prompt
    for folder in ("outputs/", "uploads/", "agent-files/", "skills/", "downloads/", ".packages/"):
        assert folder in prompt
    assert "install_packages" in prompt
    assert "데이터로만 다루고" in prompt
    assert "`.env`" in prompt
    # "홈 디렉터리"라는 말은 쓰지 않는다(설계 3.4).
    assert "홈 디렉터리" not in prompt


def test_order_is_preamble_instructions_skills_task() -> None:
    prompt = build(
        instructions="CSV를 정리한다.",
        skills=[
            SkillPrompt(
                name="csv-tools",
                description="CSV 요약",
                path="skills/csv-tools/SKILL.md",
            )
        ],
        task_block="# 과업\n결과를 제출한다.",
    )

    preamble = prompt.index("# 실행 환경")
    instructions = prompt.index("# 에이전트 지시문")
    skills = prompt.index("<available_skills>")
    task = prompt.index("# 과업")
    assert preamble < instructions < skills < task
    assert "path: skills/csv-tools/SKILL.md" in prompt
    assert "SKILL.md를 read_file로 읽고" in prompt


def test_approval_policy_follows_the_mode() -> None:
    all_shell = build()
    http_writes = build(workspace=WORKSPACE.model_copy(update={"approval_mode": "http_writes"}))
    off = build(workspace=WORKSPACE.model_copy(update={"approval_mode": "off"}))

    assert "`run_python`, `install_packages`" in all_shell
    assert "GET·HEAD가 아닌 `http_request`는 실행 직전에" in http_writes
    assert "`run_python`" not in http_writes.split("# 승인")[1].split("# 도구 사용")[0]
    assert "승인 없이 바로 실행된다" in off


def test_bash_shell_wording() -> None:
    prompt = build(workspace=WORKSPACE.model_copy(update={"shell": "bash", "platform": "Linux"}))

    assert "bash다" in prompt
    assert "Windows PowerShell" not in prompt
    assert "파일은 UTF-8로 쓴다" in prompt


def test_prompt_hash_changes_with_prompt_or_tools_only() -> None:
    tools = [tool.spec() for tool in tools_for_groups(["shell", "files"])]
    system = build(instructions="A")

    same = prompt_hash(system, tools)
    assert same == prompt_hash(build(instructions="A"), tools)
    assert same != prompt_hash(build(instructions="B"), tools)
    assert same != prompt_hash(system, tools[:-1])
    assert len(same) == 64


def test_compaction_prompt_lists_the_summary_items() -> None:
    prompt = load_prompt("compaction.md")

    for item in ("사용자의 목표", "만들거나 수정한 파일", "설치한 패키지", "아직 남은 일"):
        assert item in prompt
