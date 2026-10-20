"""텍스트 렌더러(2단계 설계 3.3, 9.3)와 1단계 render_instruction의 strict 환경."""

from typing import Any

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.agent.task_service import TaskSpecError, render_instruction
from app.workflow import render
from app.workflow.render import TemplateError, template_names
from app.workflow.variables import ScopeView

FILE = {
    "file_id": "f1",
    "filename": "보고서.pdf",
    "mime": "application/pdf",
    "size_bytes": 1,
    "storage_key": "k",
    "source": "wf_file",
}


def context() -> dict[str, Any]:
    return {
        "llm_1": ScopeView(
            "llm_1",
            {"text": "안녕", "items": [1, 2], "json": {"a": 1}, "flag": True, "none": None},
        ),
        "skipped_1": ScopeView("skipped_1", {}, nullable=True),
        "start": ScopeView("start", {"doc": FILE, "__is_success": True}),
        "sys": ScopeView("sys", {"query": "질문"}),
    }


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{ llm_1.text }}", "안녕"),
        # 출력 이름이 dict 메서드 이름과 같아도 출력이 잡힌다.
        ("{{ llm_1.items }}", "[1,2]"),
        ("{{ llm_1.json }}", '{"a":1}'),
        ("{{ llm_1.none }}|{{ skipped_1.text }}|", "||"),
        ("{{ llm_1.flag }}", "True"),
        ("{{ start.doc }}", "보고서.pdf"),
        ("{{ start['__is_success'] }}", "True"),
        ("{{ llm_1.json | tojson }}", '{"a": 1}'),
        ("{{ '{\"b\": [1]}' | fromjson }}", '{"b":[1]}'),
        ("{{ 'a-b-c' | regex_replace('-', '+') }}", "a+b+c"),
        ("{{ 'abcdefghij' | truncate_tokens(2) }}", "abcdefg"),
        ("{% for x in llm_1.items %}{{ loop.index }}{{ x }}{% endfor %}", "1122"),
        ("{{ len(llm_1.items) }} {{ dict(a=1) }} {{ range(3) | list }}", '2 {"a":1} [0,1,2]'),
        ("{{ sys.query }}", "질문"),
    ],
)
def test_lenient_rendering(template: str, expected: str) -> None:
    rendered = render.render(template, context())

    assert rendered.text == expected
    assert rendered.missing == ()


def test_missing_variables_render_empty_and_are_reported() -> None:
    rendered = render.render("[{{ ghost }}][{{ llm_1.nope }}][{{ ghost.a.b }}]", context())

    assert rendered.text == "[][][]"
    assert rendered.missing == ("ghost", "llm_1.nope")


def test_values_are_data_not_templates() -> None:
    rendered = render.render("{{ sys.query }}", {"sys": ScopeView("sys", {"query": "{{ 7*7 }}"})})

    assert rendered.text == "{{ 7*7 }}"


@pytest.mark.parametrize(
    ("template", "code"),
    [
        ("{{ ''.__class__ }}", "template_forbidden"),
        ("{{ ''.__class__.__mro__ }}", "template_forbidden"),
        ("{% set xs = [1] %}{{ xs.append(2) }}", "template_forbidden"),
        ("{{ range(10001) }}", "template_limit"),
        (
            "{% for a in range(1000) %}{% for b in range(1000) %}{% endfor %}{% endfor %}",
            "template_limit",
        ),
        ("{{ 'a' * 2000000 }}", "template_limit"),
        ("{{ 3 ** 100000 }}", "template_limit"),
        ("{% for i in range(2000) %}{{ 'x' * 1000 }}{% endfor %}", "template_limit"),
        ("{{ 'x' | regex_replace('(', '') }}", "template_error"),
        ("{{ 'nope' | fromjson }}", "template_error"),
        ("{{ lipsum() }}", "template_error"),
        ("{{ 1 / 0 }}", "template_error"),
        ("{% for x in %}", "template_syntax"),
        ("{% include 'other.txt' %}", "template_forbidden"),
        ("{% import 'os' as os %}", "template_forbidden"),
        ("{% from 'm.txt' import x %}", "template_forbidden"),
        ("{% extends 'base.txt' %}", "template_forbidden"),
    ],
)
def test_sandbox_and_limits(template: str, code: str) -> None:
    with pytest.raises(TemplateError) as caught:
        render.render(template, context())

    assert caught.value.code == code


def test_template_names_for_validation() -> None:
    names = template_names(
        "{{ a.b }} {{ c['d'] }} {% for x in e %}{{ x.y }}{{ loop.index }}{% endfor %}"
        "{% set z = 1 %}{{ z }} {{ range(2) }} {% macro m(p) %}{{ p }}{% endmacro %}{{ m(1) }}"
    )

    assert names.names == frozenset({"a", "c", "e"})
    assert names.attrs == frozenset({("a", "b"), ("c", "d")})
    with pytest.raises(TemplateError) as caught:
        template_names("{{ a ")
    assert caught.value.code == "template_syntax"


def test_environments_are_built_once_and_strict_is_the_sandboxed_strict_one() -> None:
    render.setup()
    strict = render.strict_environment()

    assert strict is render.strict_environment()
    assert render.lenient_environment() is render.lenient_environment()
    assert isinstance(strict, ImmutableSandboxedEnvironment)
    assert strict.undefined is StrictUndefined
    # strict는 1단계와 같은 Jinja2 기본 전역·필터를 쓴다.
    assert "lipsum" in strict.globals
    assert "lipsum" not in render.lenient_environment().globals


def test_render_instruction_keeps_its_behaviour() -> None:
    assert render_instruction("{{ a }}+{{ b }}", {"a": 1, "b": [2]}) == "1+[2]"
    assert render_instruction("{{ data | tojson }}", {"data": {"b": 1, "a": "<"}}) == (
        '{"a": "\\u003c", "b": 1}'
    )
    with pytest.raises(TaskSpecError) as caught:
        render_instruction("{{ missing }}", {})
    assert caught.value.code == "instruction_invalid"
