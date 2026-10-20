"""워크플로우 DSL 내보내기·가져오기 API(2단계 설계 5장, 6.1, M18).

우리 YAML 라운드트립(한글 이름·이모지 아이콘), include_secrets 양쪽, Dify 예제 셋의
unsupported·경고, 2MB·깨진 YAML 거부, agent 노드의 slug 복원과 agent_not_found, 가져온 code 노드의
검토 전 실행·발행 차단과 초안 저장으로 검토를 마치는 흐름을 본다.
"""

import base64
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.config import Settings
from app.workflow.dsl.import_ import parse_yaml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dsl"
FETCH = {"X-Requested-With": "fetch"}
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # 비밀 환경 변수를 저장·내보내려면 ENCRYPTION_KEY가 있어야 한다.
    return settings.model_copy(update={"encryption_key": SecretStr(ENCRYPTION_KEY)})


@pytest.fixture
async def api(logged_in: AsyncClient) -> AsyncClient:
    return logged_in


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def import_json(api: AsyncClient, text: str, **params: str) -> Response:
    return await api.post("/api/console/workflows/import", json={"yaml": text}, params=params)


async def import_file(api: AsyncClient, data: bytes, **params: str) -> Response:
    return await api.post(
        "/api/console/workflows/import",
        files={"file": ("workflow.yml", data, "application/yaml")},
        headers=FETCH,
        params=params,
    )


async def export(api: AsyncClient, workflow_id: str, **params: str) -> Response:
    return await api.post(
        f"/api/console/workflows/{workflow_id}/export", headers=FETCH, params=params
    )


async def draft(api: AsyncClient, workflow_id: str) -> dict[str, Any]:
    response = await api.get(f"/api/console/workflows/{workflow_id}/draft")
    assert response.status_code == 200, response.text
    return response.json()


async def save(api: AsyncClient, workflow_id: str, **fields: Any) -> Response:
    current = await draft(api, workflow_id)
    body = {
        "graph": current["graph"],
        "env_vars": [
            {"name": item["name"], "value": item["value"], "secret": item["secret"]}
            for item in current["env_vars"]
        ],
        "conversation_vars": current["conversation_vars"],
        "settings": current["settings"],
        "updated_at": current["updated_at"],
        **fields,
    }
    return await api.put(f"/api/console/workflows/{workflow_id}/draft", json=body)


def codes(items: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(item["code"], item.get("node_id")) for item in items]


async def test_export_then_import_round_trip_keeps_graph_and_korean_text(api: AsyncClient) -> None:
    created = await import_json(api, fixture("ours/workflow_branches.yaml"))

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["issues"] == []
    assert body["converted"] == {"source": "dsl", "nodes": 10, "unsupported": [], "warnings": []}
    first_id = body["workflow_id"]
    workflow = (await api.get(f"/api/console/workflows/{first_id}")).json()
    assert (workflow["name"], workflow["icon"], workflow["slug"]) == (
        "인사 보내기",
        "👋",
        "workflow",
    )

    exported = await export(api, first_id)

    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "application/yaml; charset=utf-8"
    disposition = exported.headers["content-disposition"]
    # 한글을 못 읽는 클라이언트용 이름은 slug다.
    assert disposition.startswith('attachment; filename="workflow.yml";')
    assert "filename*=UTF-8''%EC%9D%B8%EC%82%AC%20%EB%B3%B4%EB%82%B4%EA%B8%B0.yml" in disposition
    assert "x-dsl-warning" not in exported.headers
    text = exported.content.decode("utf-8")
    assert "name: 인사 보내기\n" in text and "icon: 👋\n" in text

    again = await import_file(api, exported.content)

    assert again.status_code == 201, again.text
    second_id = again.json()["workflow_id"]
    assert second_id != first_id
    assert (await draft(api, second_id))["graph"] == (await draft(api, first_id))["graph"]
    second = (await api.get(f"/api/console/workflows/{second_id}")).json()
    assert second["slug"] == "workflow-2" and second["name"] == "인사 보내기"
    assert (await export(api, second_id)).content == exported.content


async def test_import_into_overwrites_only_the_draft(api: AsyncClient) -> None:
    target = (await api.post("/api/console/workflows", json={"name": "덮어쓸 곳"})).json()
    before = await draft(api, target["id"])
    published = await api.post(f"/api/console/workflows/{target['id']}/publish", json={})
    assert published.status_code == 201, published.text

    response = await import_json(api, fixture("ours/workflow_iteration.yaml"), into=target["id"])

    assert response.status_code == 200, response.text
    assert response.json()["workflow_id"] == target["id"]
    after = await draft(api, target["id"])
    assert [node["id"] for node in after["graph"]["nodes"]] == [
        "start",
        "split",
        "each",
        "each_start",
        "summarize",
        "done",
    ]
    assert after["updated_at"] != before["updated_at"]
    workflow = (await api.get(f"/api/console/workflows/{target['id']}")).json()
    # 이름·발행 버전은 그대로다.
    assert workflow["name"] == "덮어쓸 곳"
    assert workflow["published_version_id"] == published.json()["version"]["id"]
    version = (
        await api.get(
            f"/api/console/workflows/{target['id']}/versions/{workflow['published_version_id']}"
        )
    ).json()
    assert [node["type"] for node in version["graph"]["nodes"]] == ["start", "end"]

    chatflow = await import_json(api, fixture("ours/chatflow_all_nodes.yaml"), into=target["id"])
    assert chatflow.status_code == 409 and chatflow.json()["error"]["code"] == "kind_mismatch"
    missing = await import_json(
        api, fixture("ours/workflow_iteration.yaml"), into=str(uuid.uuid4())
    )
    assert missing.status_code == 404


async def test_secrets_are_left_out_unless_asked(api: AsyncClient) -> None:
    workflow_id = (await api.post("/api/console/workflows", json={"name": "비밀"})).json()["id"]
    saved = await save(
        api,
        workflow_id,
        env_vars=[
            {"name": "PLAIN", "value": "https://example.com", "secret": False},
            {"name": "TOKEN", "value": "tok-1234567890", "secret": True},
        ],
    )
    assert saved.status_code == 200, saved.text

    hidden = await export(api, workflow_id)
    shown = await export(api, workflow_id, include_secrets="true")

    assert "tok-1234567890" not in hidden.text
    assert "x-dsl-warning" not in hidden.headers
    assert parse_yaml(hidden.text)["env_vars"] == [
        {"name": "PLAIN", "value": "https://example.com", "secret": False},
        {"name": "TOKEN", "secret": True},
    ]
    assert shown.headers["x-dsl-warning"] == "secrets_included"
    assert parse_yaml(shown.text)["env_vars"][1] == {
        "name": "TOKEN",
        "value": "tok-1234567890",
        "secret": True,
    }

    with_value = await import_file(api, shown.content)
    without_value = await import_file(api, hidden.content)

    assert with_value.json()["converted"]["warnings"] == []
    token = (await draft(api, with_value.json()["workflow_id"]))["env_vars"][1]
    assert token == {"name": "TOKEN", "secret": True, "value": None, "has_value": True}
    empty_id = without_value.json()["workflow_id"]
    assert codes(without_value.json()["converted"]["warnings"]) == [("env_value_missing", None)]
    empty = (await draft(api, empty_id))["env_vars"][1]
    assert empty == {"name": "TOKEN", "secret": True, "value": None, "has_value": False}
    # 값이 빈 채로 들어온 변수도 그대로 다시 저장할 수 있다(화면 자동 저장).
    resaved = await save(api, empty_id)
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["env_vars"][1]["has_value"] is False
    # 같은 이름의 비밀값이 있는 초안에 값 없는 파일을 덮어쓰면 저장된 값을 남긴다.
    kept = await import_file(api, hidden.content, into=workflow_id)
    assert kept.status_code == 200, kept.text
    assert (await draft(api, workflow_id))["env_vars"][1]["has_value"] is True


DIFY_CASES = [
    (
        "dify/workflow_search.yaml",
        "workflow",
        [],
        {"model_replaced", "dependencies_ignored", "imported_code", "env_value_missing"},
        {"imported_code_unreviewed"},
    ),
    # tool·list-operator·document-extractor는 M19부터 우리 노드로 들어온다.
    (
        "dify/chatflow_support.yaml",
        "chatflow",
        ["n1730000000003"],
        {"model_replaced", "unsupported_node", "features_ignored"},
        {"unsupported_node"},
    ),
    (
        "dify/workflow_batch.yaml",
        "workflow",
        ["n2"],
        {"model_replaced", "unsupported_node", "iteration_parallel", "edge_dropped"},
        {"unsupported_node"},
    ),
    (
        "dify/workflow_second_nodes.yaml",
        "workflow",
        ["n603"],
        {"loop_count", "tool_param_dropped", "human_input_settings", "unsupported_node"},
        {"unsupported_node"},
    ),
]


@pytest.mark.parametrize(("name", "kind", "unsupported", "warned", "issues"), DIFY_CASES)
async def test_dify_examples_come_in_with_unsupported_nodes_and_warnings(
    api: AsyncClient,
    name: str,
    kind: str,
    unsupported: list[str],
    warned: set[str],
    issues: set[str],
) -> None:
    response = await import_file(api, fixture(name).encode("utf-8"))

    assert response.status_code == 201, response.text
    body = response.json()
    converted = body["converted"]
    assert converted["source"] == "dify"
    assert converted["unsupported"] == unsupported
    assert warned <= {item["code"] for item in converted["warnings"]}
    assert {item["code"] for item in body["issues"]} == issues
    workflow = (await api.get(f"/api/console/workflows/{body['workflow_id']}")).json()
    assert workflow["kind"] == kind
    stored = await draft(api, body["workflow_id"])
    assert stored["issues"] == body["issues"]
    assert len(stored["graph"]["nodes"]) == converted["nodes"]
    assert [node["id"] for node in stored["graph"]["nodes"] if node["type"] == "unsupported"] == (
        unsupported
    )
    # 지원하지 않는 노드가 있어도 내보낸 우리 YAML은 다시 들어온다.
    again = await import_file(api, (await export(api, body["workflow_id"])).content)
    assert again.status_code == 201, again.text
    assert again.json()["converted"]["source"] == "dsl"
    assert (await draft(api, again.json()["workflow_id"]))["graph"] == stored["graph"]


async def test_large_or_broken_files_are_refused(api: AsyncClient) -> None:
    oversized = "#" * (2 * 1024 * 1024 + 1)
    refused = [
        (await import_json(api, oversized), 422, "dsl_too_large"),
        (await import_file(api, oversized.encode()), 422, "dsl_too_large"),
        (await import_file(api, b"#" * (3 * 1024 * 1024)), 422, "dsl_too_large"),
        (await import_json(api, "name: [unclosed\n"), 422, "invalid_yaml"),
        (await import_json(api, "- a\n- b\n"), 422, "invalid_yaml"),
        (await import_file(api, "name: 한글".encode("cp949")), 422, "invalid_yaml"),
        (await import_json(api, "title: 무엇도 아닌 YAML\n"), 422, "unknown_dsl_format"),
        (
            await import_json(
                api,
                fixture("ours/workflow_branches.yaml").replace("dsl_version: 1", "dsl_version: 9"),
            ),
            422,
            "unsupported_dsl_version",
        ),
        (await import_json(api, "dsl_version: 1\nkind: x\n"), 422, "invalid_dsl"),
        (await api.post("/api/console/workflows/import", json={}), 422, "validation_error"),
        (
            await api.post(
                "/api/console/workflows/import",
                content=b"dsl_version: 1",
                headers={**FETCH, "content-type": "application/yaml"},
            ),
            415,
            "unsupported_media_type",
        ),
    ]

    for response, status, code in refused:
        assert (response.status_code, response.json()["error"]["code"]) == (status, code), (
            response.text
        )
    broken = refused[3][0].json()["error"]
    assert broken["details"]["line"] == 2
    assert (await api.get("/api/console/workflows")).json()["items"] == []


async def make_agent(api: AsyncClient, slug: str) -> str:
    agent = await api.post("/api/console/agents", json={"name": "도우미", "slug": slug})
    assert agent.status_code == 201, agent.text
    published = await api.post(f"/api/console/agents/{agent.json()['id']}/publish", json={})
    assert published.status_code == 201, published.text
    return agent.json()["id"]


def agent_flow(agent_id: str) -> str:
    return f"""
dsl_version: 1
kind: workflow
name: 에이전트 호출
graph:
  nodes:
    - {{ id: start, type: start, version: 1, config: {{ fields: [] }} }}
    - id: helper_1
      type: agent
      version: 1
      config: {{ agent_id: "{agent_id}", instruction: 오늘 할 일을 정리한다 }}
    - id: end_1
      type: end
      version: 1
      config: {{ outputs: [{{ name: answer, var: helper_1.text }}] }}
  edges:
    - {{ id: e1, source: start, target: helper_1 }}
    - {{ id: e2, source: helper_1, target: end_1 }}
"""


async def test_agent_nodes_find_their_agent_by_slug(api: AsyncClient) -> None:
    agent_id = await make_agent(api, "helper")
    created = await import_json(api, agent_flow(agent_id))
    assert created.status_code == 201, created.text
    assert created.json()["issues"] == []
    workflow_id = created.json()["workflow_id"]

    text = (await export(api, workflow_id)).text
    assert "agent_slug: helper" in text

    # 다른 PC에서 내보낸 파일처럼 id만 바꾼다. slug로 이 PC의 에이전트를 찾는다.
    foreign = text.replace(agent_id, str(uuid.uuid4()))
    relinked = await import_json(api, foreign)
    assert relinked.json()["issues"] == []
    assert codes(relinked.json()["converted"]["warnings"]) == [("agent_relinked", "helper_1")]
    node = (await draft(api, relinked.json()["workflow_id"]))["graph"]["nodes"][1]
    assert node["config"]["agent_id"] == agent_id and "agent_slug" not in node["config"]

    lost = await import_json(api, foreign.replace("agent_slug: helper", "agent_slug: nobody"))
    assert codes(lost.json()["converted"]["warnings"]) == [("agent_not_found", "helper_1")]
    assert ("agent_not_found", "helper_1") in codes(lost.json()["issues"])

    # 에이전트를 지우면 처음 워크플로우도 같은 검증 오류로 실행·발행이 막힌다.
    deleted = await api.delete(f"/api/console/agents/{agent_id}", headers=FETCH)
    assert deleted.status_code == 204
    assert ("agent_not_found", "helper_1") in codes((await draft(api, workflow_id))["issues"])
    refused = await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert refused.status_code == 422
    assert ("agent_not_found", "helper_1") in codes(refused.json()["error"]["details"])
    run = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    assert run.status_code == 422 and run.json()["error"]["code"] == "draft_invalid"


CODE_FLOW = """
dsl_version: 1
kind: workflow
name: 코드 한 줄
settings: { approval_mode: "off" }
graph:
  nodes:
    - { id: start, type: start, version: 1, config: { fields: [] } }
    - id: calc
      type: code
      version: 1
      config:
        code: "def main() -> dict:\\n    return {'n': 1}\\n"
        outputs: [{ name: n, type: number }]
    - { id: end_1, type: end, version: 1, config: { outputs: [{ name: n, var: calc.n }] } }
  edges:
    - { id: e1, source: start, target: calc }
    - { id: e2, source: calc, target: end_1 }
"""


async def test_imported_code_is_blocked_until_reviewed(api: AsyncClient) -> None:
    response = await import_json(api, CODE_FLOW)

    assert response.status_code == 201, response.text
    body = response.json()
    workflow_id = body["workflow_id"]
    assert codes(body["issues"]) == [("imported_code_unreviewed", "calc")]
    assert codes(body["converted"]["warnings"]) == [
        ("imported_code", None),
        ("approval_mode_reset", None),
    ]
    stored = await draft(api, workflow_id)
    assert stored["settings"]["approval_mode"] == "inherit"
    assert stored["graph"]["nodes"][1]["ui"] == {"imported_code": True}
    run = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    assert run.status_code == 422 and run.json()["error"]["code"] == "draft_invalid"
    assert codes(run.json()["error"]["details"]) == [("imported_code_unreviewed", "calc")]
    single = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/nodes/calc/runs", json={"inputs": {}}
    )
    assert single.status_code == 422
    published = await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 422

    # 검토 완료: 화면이 노드의 ui에서 imported_code를 지우고 초안을 저장한다.
    graph = stored["graph"]
    graph["nodes"][1]["ui"] = {}
    reviewed = await save(api, workflow_id, graph=graph)

    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["issues"] == []
    published = await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text


async def test_export_picks_a_version(api: AsyncClient) -> None:
    created = await import_json(api, fixture("ours/workflow_branches.yaml"))
    workflow_id = created.json()["workflow_id"]
    none_yet = await export(api, workflow_id, version="published")
    assert none_yet.status_code == 404
    first = await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert first.status_code == 201, first.text
    version_id = first.json()["version"]["id"]
    current = await draft(api, workflow_id)
    graph = current["graph"]
    graph["nodes"][2]["title"] = "바뀐 제목"
    assert (await save(api, workflow_id, graph=graph)).status_code == 200

    by_name = await export(api, workflow_id, version="published")
    by_id = await export(api, workflow_id, version=version_id)
    latest = await export(api, workflow_id)

    assert by_name.status_code == 200 and by_name.content == by_id.content
    assert "바뀐 제목" not in by_name.text and "바뀐 제목" in latest.text
    assert (await export(api, workflow_id, version=str(uuid.uuid4()))).status_code == 404
    wrong = await export(api, workflow_id, version="latest")
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "validation_error"
    assert (await export(api, str(uuid.uuid4()))).status_code == 404
