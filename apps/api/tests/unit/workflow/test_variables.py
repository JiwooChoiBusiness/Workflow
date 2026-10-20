"""변수 풀·선택자·런타임 변환·크기 상한·마스킹(2단계 설계 3.2, 9.4)."""

from typing import Any

import pytest

from app.workflow.variables import (
    MASK,
    Masker,
    PoolLimitError,
    PoolView,
    Selector,
    SelectorError,
    StreamMasker,
    VariableError,
    VariablePool,
    assignable,
    coerce,
    measure,
    parse_selector,
    preview,
    scope_values,
    type_matches,
)

FILE = {
    "file_id": "f1",
    "filename": "a.txt",
    "mime": "text/plain",
    "size_bytes": 1,
    "storage_key": "k",
    "source": "wf_file",
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("llm_1.text", Selector("llm_1", "text")),
        ("http_1.json.items", Selector("http_1", "json", ("items",))),
        ("http_1.json.items[0].name", Selector("http_1", "json", ("items", 0, "name"))),
        ("http_1.headers.content-type", Selector("http_1", "headers", ("content-type",))),
        ("sys.query", Selector("sys", "query")),
        ("extractor_1.__is_success", Selector("extractor_1", "__is_success")),
        (" env.API_BASE ", Selector("env", "API_BASE")),
    ],
)
def test_selectors_parse_scope_name_and_path(text: str, expected: Selector) -> None:
    assert parse_selector(text) == expected
    assert str(expected) == text.strip()


@pytest.mark.parametrize(
    "text", ["llm_1", "", "LLM.text", "llm_1.", "llm_1.text[", "1abc.text", "{{#llm_1.text#}}"]
)
def test_bad_selectors_are_rejected(text: str) -> None:
    with pytest.raises(SelectorError):
        parse_selector(text)


@pytest.mark.parametrize(
    ("value", "expected", "result"),
    [
        (3, "string", "3"),
        (True, "string", "True"),
        ("3", "number", 3),
        (" 2.5 ", "number", 2.5),
        ("1e3", "number", 1000.0),
        ("x", "array[any]", ["x"]),
        ([1], "array[any]", [1]),
        (None, "number", None),
        ({"a": 1}, "object", {"a": 1}),
        (FILE, "file", FILE),
        (FILE, "object", FILE),
        ("anything", "any", "anything"),
    ],
)
def test_runtime_conversion_rules(value: Any, expected: Any, result: Any) -> None:
    assert coerce(value, expected) == result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "number"),
        ("nan", "number"),
        (True, "number"),
        (1, "boolean"),
        ("x", "array[string]"),
        ([1, "a"], "array[number]"),
        ({"a": 1}, "string"),
        ({"a": 1}, "file"),
    ],
)
def test_other_conversions_are_type_mismatch(value: Any, expected: Any) -> None:
    with pytest.raises(VariableError) as caught:
        coerce(value, expected)

    assert caught.value.code == "type_mismatch"


def test_type_checks_and_assignability() -> None:
    assert type_matches([FILE], "array[file]")
    assert not type_matches(1.0 * float("inf"), "number")
    assert assignable("number", "string") == "yes"
    assert assignable("string", "number") == "maybe"
    assert assignable("object", "number") == "no"
    assert assignable(None, "number") == "yes"


@pytest.mark.parametrize(
    "value",
    [
        "가" * 400_000,
        {"k": "x" * 600_000, "l": "y" * 600_000},
        list(range(10_001)),
        {
            "a": {
                "a": {
                    "a": {
                        "a": {
                            "a": {
                                "a": {
                                    "a": {
                                        "a": {
                                            "a": {
                                                "a": {
                                                    "a": {
                                                        "a": {
                                                            "a": {
                                                                "a": {
                                                                    "a": {
                                                                        "a": {
                                                                            "a": {
                                                                                "a": {
                                                                                    "a": {
                                                                                        "a": {
                                                                                            "a": 1
                                                                                        }
                                                                                    }
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    ],
    # 값이 그대로 id가 되면 PYTEST_CURRENT_TEST 환경 변수 상한(Windows 32767자)을 넘는다.
    ids=["string_1mb", "object_1mb", "array_10k", "depth_21"],
)
def test_variable_size_limits(value: Any) -> None:
    with pytest.raises(VariableError) as caught:
        measure(value)

    assert caught.value.code == "variable_limit"


def test_pool_outputs_are_written_once_and_skipped_nodes_read_null() -> None:
    pool = VariablePool()
    pool.put_outputs("llm_1", {"text": "안녕"}, {"text": "string"})
    pool.mark_skipped("llm_2")
    pool.mark_empty("http_1")
    view = PoolView(pool)

    assert view.lookup(parse_selector("llm_1.text")).value == "안녕"
    assert view.lookup(parse_selector("llm_1.text")).type == "string"
    skipped = view.lookup(parse_selector("llm_2.text"))
    assert skipped.found and skipped.skipped and skipped.value is None
    empty = view.lookup(parse_selector("http_1.body"))
    assert empty.found and not empty.skipped and empty.value is None
    assert not view.lookup(parse_selector("llm_1.nope")).found
    assert not view.lookup(parse_selector("ghost.text")).found
    with pytest.raises(ValueError):
        pool.put_outputs("llm_1", {"text": "다시"})
    with pytest.raises(ValueError):
        pool.put_outputs("llm_2", {"text": "다시"})


def test_paths_walk_objects_and_arrays() -> None:
    pool = VariablePool()
    pool.put_outputs("http_1", {"json": {"items": [{"name": "a"}, {"name": "b"}]}})
    view = PoolView(pool)

    assert view.lookup(parse_selector("http_1.json.items[1].name")).value == "b"
    missing = view.lookup(parse_selector("http_1.json.items[5].name"))
    assert not missing.found and missing.value is None


def test_iteration_frames_store_per_index_without_copying_the_pool() -> None:
    pool = VariablePool()
    pool.put_outputs("start", {"q": "질문"})
    pool.put_outputs("template_2@0", {"output": "첫째"})
    pool.put_outputs("template_2@1", {"output": "둘째"})
    inner = {"iteration_start_1", "template_2"}

    first = PoolView(pool, inner=inner, index=0)
    second = PoolView(pool, inner=inner, index=1)
    root = PoolView(pool)

    assert first.lookup(parse_selector("template_2.output")).value == "첫째"
    assert second.lookup(parse_selector("template_2.output")).value == "둘째"
    assert second.lookup(parse_selector("start.q")).value == "질문"
    assert not root.lookup(parse_selector("template_2.output")).found
    context = second.template_context()
    assert scope_values(context["template_2"]) == {"output": "둘째"}
    assert set(context) == {"start", "template_2"}
    assert pool.dump() == {
        "start.q": "질문",
        "template_2@0.output": "첫째",
        "template_2@1.output": "둘째",
    }


def test_pool_size_is_tracked_on_insert_and_conversation_updates_replace() -> None:
    pool = VariablePool(max_bytes=200)
    pool.put("conv", "memo", "x" * 50, "string")
    size = pool.size
    pool.assign("memo", "y" * 60)

    assert pool.size == size + 10
    assert pool.lookup("conv", "memo").type == "string"
    with pytest.raises(PoolLimitError):
        pool.put_outputs("big", {"v": "z" * 300})
    assert pool.scope("big") is None


def test_dump_and_restore_round_trip() -> None:
    pool = VariablePool()
    pool.put("sys", "run_id", "r1", "string")
    pool.put("env", "TOKEN", "secret-value")
    pool.put_outputs("llm_1", {"text": "t"})
    pool.mark_skipped("llm_2")
    restored = VariablePool()

    restored.restore(pool.dump(exclude=("env",)), skipped=pool.skipped, empty=pool.empty)

    assert restored.dump() == {"sys.run_id": "r1", "llm_1.text": "t"}
    assert restored.is_skipped("llm_2")


def test_masker_hides_long_secrets_everywhere_and_reports_short_ones() -> None:
    masker = Masker(["password-1234", "short", "", 42])

    masked = masker.mask({"a": "x password-1234 y", "b": ["password-1234"], "c": 1})

    assert masked == {"a": f"x {MASK} y", "b": [MASK], "c": 1}
    assert masker.short == ("short",)
    assert masker.text("short") == "short"
    assert Masker().mask({"a": "password-1234"}) == {"a": "password-1234"}


def test_stream_masker_hides_secrets_split_across_deltas() -> None:
    hidden = "tok-1234567890"
    stream = StreamMasker(Masker([hidden, "other-secret-9"]))
    text = f"키는 {hidden} 이고 끝은 tok-12"

    sent = [stream.feed(text[start : start + 4]) for start in range(0, len(text), 4)]
    rest = stream.flush()

    assert "".join(sent) + rest == f"키는 {MASK} 이고 끝은 tok-12"
    assert all(hidden[:5] not in piece for piece in sent)
    # 비밀값의 앞부분으로 끝난 글은 스트림이 끝날 때 그대로 나간다.
    assert rest == "tok-12"
    assert stream.flush() == ""
    plain = StreamMasker(Masker())
    assert [plain.feed("tok-1"), plain.feed("234567890")] == ["tok-1", "234567890"]


def test_preview_keeps_small_values_and_cuts_large_ones() -> None:
    small, cut = preview({"a": 1, "b": "짧다"})
    assert small == {"a": 1, "b": "짧다"} and cut is False

    large, cut = preview({"a": "가" * 3000, "b": {"x": "y" * 5000}, "c": 1}, limit=4096)
    assert cut is True
    assert len(str(large).encode("utf-8")) < 4096 + 200
    assert large["a"].startswith("가")
