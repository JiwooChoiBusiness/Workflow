from collections.abc import Iterable

from app.agent.tools.ask_user import AskUserTool
from app.agent.tools.base import Tool, ToolGroup
from app.agent.tools.edit_file import EditFileTool
from app.agent.tools.http_request import HttpRequestTool
from app.agent.tools.install_packages import InstallPackagesTool
from app.agent.tools.list_files import ListFilesTool
from app.agent.tools.read_file import ReadFileTool
from app.agent.tools.run_python import RunPythonTool
from app.agent.tools.shell import ShellTool
from app.agent.tools.write_file import WriteFileTool

# 모델에 보내는 순서이기도 하다. 순서가 바뀌면 prompt_hash가 바뀐다.
BUILTIN_TOOLS: tuple[Tool, ...] = (
    ShellTool(),
    RunPythonTool(),
    InstallPackagesTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    ListFilesTool(),
    HttpRequestTool(),
    AskUserTool(),
)


def tools_for_groups(groups: Iterable[str], *, exclude: Iterable[str] = ()) -> list[Tool]:
    """에이전트 버전의 tools(그룹 목록)에 든 도구. 순서는 BUILTIN_TOOLS를 따른다.

    exclude의 그룹은 뺀다. call_agent로 만든 자식 런은 ask_user를 받지 않는다(오케스트레이션 3.5).
    """
    wanted: set[str] = set(groups) - set(exclude)
    return [tool for tool in BUILTIN_TOOLS if tool.group in wanted]


__all__ = ["BUILTIN_TOOLS", "Tool", "ToolGroup", "tools_for_groups"]
