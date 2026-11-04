from collections.abc import Iterable

from app.agent.tools.ask_user import AskUserTool
from app.agent.tools.base import Tool, ToolGroup
from app.agent.tools.describe_sql_source import DescribeSqlSourceTool
from app.agent.tools.edit_file import EditFileTool
from app.agent.tools.http_request import HttpRequestTool
from app.agent.tools.install_packages import InstallPackagesTool
from app.agent.tools.list_files import ListFilesTool
from app.agent.tools.query_sql import QuerySqlTool
from app.agent.tools.read_file import ReadFileTool
from app.agent.tools.run_python import RunPythonTool
from app.agent.tools.search_knowledge import SearchKnowledgeTool
from app.agent.tools.shell import ShellTool
from app.agent.tools.write_file import WriteFileTool

# 모델에 보내는 순서이기도 하다. 순서가 바뀌면 prompt_hash가 바뀐다. search_knowledge(그룹
# knowledge)와 SQL 도구 둘(그룹 sql)은 끝에 두어 지식 베이스·데이터 소스가 없는 에이전트의
# prompt_hash를 바꾸지 않는다. 런에는 목록을 담은 인스턴스를 러너가 따로 붙인다.
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
    SearchKnowledgeTool(),
    DescribeSqlSourceTool(),
    QuerySqlTool(),
)


def tools_for_groups(groups: Iterable[str], *, exclude: Iterable[str] = ()) -> list[Tool]:
    """에이전트 버전의 tools(그룹 목록)에 든 도구. 순서는 BUILTIN_TOOLS를 따른다.

    exclude의 그룹은 뺀다. call_agent로 만든 자식 런은 ask_user를 받지 않는다(오케스트레이션 3.5).
    """
    wanted: set[str] = set(groups) - set(exclude)
    return [tool for tool in BUILTIN_TOOLS if tool.group in wanted]


__all__ = ["BUILTIN_TOOLS", "Tool", "ToolGroup", "tools_for_groups"]
