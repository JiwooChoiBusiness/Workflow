import io
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.skills.package import (
    MAX_SKILL_MD_BYTES,
    FrontmatterError,
    FrontmatterMissing,
    SkillPackageError,
    Structured,
    find_member,
    inspect_package,
    parse_frontmatter,
    parse_skill_md,
)

SkillZip = Callable[..., bytes]


def write(tmp_path: Path, data: bytes, name: str = "skill.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def raw_zip(entries: list[tuple[zipfile.ZipInfo | str, bytes | str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    return buffer.getvalue()


def front(body: str) -> str:
    return f"---\n{body}\n---\n본문\n"


def rejected(tmp_path: Path, data: bytes, **limits: int) -> SkillPackageError:
    with pytest.raises(SkillPackageError) as info:
        inspect_package(write(tmp_path, data), **limits)
    return info.value


class TestFrontmatter:
    def test_plain_values(self) -> None:
        values = parse_frontmatter(
            front("name: csv-tools\ndescription: CSV 파일을 다룬다  # 주석\nversion: 1.0")
        )

        assert values == {"name": "csv-tools", "description": "CSV 파일을 다룬다", "version": "1.0"}

    def test_quoted_values(self) -> None:
        values = parse_frontmatter(
            front(
                "name: 'csv-tools'\n"
                'description: "줄바꿈\\n과 \\"따옴표\\", \\u00e9"\n'
                "note: 'it''s # not a comment'"
            )
        )

        assert values["name"] == "csv-tools"
        assert values["description"] == '줄바꿈\n과 "따옴표", é'
        assert values["note"] == "it's # not a comment"

    def test_block_scalars(self) -> None:
        values = parse_frontmatter(
            front(
                "literal: |\n  첫 줄\n  둘째 줄\n\n"
                "folded: >-\n  이어지는\n  한 문장\n\n  새 문단\n"
                "plain: 여러 줄에\n  걸친 값"
            )
        )

        assert values["literal"] == "첫 줄\n둘째 줄\n"
        assert values["folded"] == "이어지는 한 문장\n새 문단"
        assert values["plain"] == "여러 줄에 걸친 값"

    def test_nested_values_are_kept_opaque(self) -> None:
        values = parse_frontmatter(
            front(
                "name: x\nmetadata:\n  owner: me\n  tags: [a, b]\n"
                "allowed-tools: [shell, files]\nempty:\nnull_value: ~"
            )
        )

        assert isinstance(values["metadata"], Structured)
        assert isinstance(values["allowed-tools"], Structured)
        assert values["empty"] is None
        assert values["null_value"] is None

    def test_bom_crlf_and_dots_terminator(self) -> None:
        values = parse_frontmatter("\ufeff---\r\nname: x\r\ndescription: y\r\n...\r\n본문")

        assert values == {"name": "x", "description": "y"}

    @pytest.mark.parametrize(
        "text",
        ["# 제목\n본문", "", "name: x\n---\n"],
        ids=["no-delimiter", "empty", "starts-with-key"],
    )
    def test_missing(self, text: str) -> None:
        with pytest.raises(FrontmatterMissing):
            parse_frontmatter(text)

    @pytest.mark.parametrize(
        "body",
        [
            "name: x\nname: y",
            "  name: x",
            "just text",
            'name: "unterminated',
            "name: 'a' trailing",
            'name: "\\q"',
            "text: |\nnot indented",
        ],
        ids=[
            "duplicate",
            "indented-first",
            "not-mapping",
            "open-quote",
            "after-quote",
            "bad-escape",
            "block-indent",
        ],
    )
    def test_invalid(self, body: str) -> None:
        with pytest.raises(FrontmatterError):
            parse_frontmatter(front(body))

    def test_unclosed(self) -> None:
        with pytest.raises(FrontmatterError, match="닫는"):
            parse_frontmatter("---\nname: x\n")


class TestSkillMd:
    def test_required_fields(self) -> None:
        parsed = parse_skill_md(front("name: csv-tools\ndescription: ' 도구 '\nversion: 2"))

        assert (parsed.name, parsed.description, parsed.version) == ("csv-tools", "도구", "2")

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ("description: x", "skill_name_invalid"),
            ("name: CSV_Tools\ndescription: x", "skill_name_invalid"),
            ("name: " + "a" * 65 + "\ndescription: x", "skill_name_invalid"),
            ("name: x", "skill_description_invalid"),
            ("name: x\ndescription: ''", "skill_description_invalid"),
            ("name: x\ndescription: " + "가" * 1025, "skill_description_invalid"),
            ("name: x\ndescription: y\nversion:\n  major: 1", "skill_version_invalid"),
        ],
    )
    def test_rejects(self, body: str, code: str) -> None:
        with pytest.raises(SkillPackageError) as info:
            parse_skill_md(front(body))
        assert info.value.code == code

    def test_description_limit_is_inclusive(self) -> None:
        parsed = parse_skill_md(front("name: x\ndescription: " + "가" * 1024))

        assert len(parsed.description) == 1024

    def test_missing_frontmatter_has_its_own_code(self) -> None:
        with pytest.raises(SkillPackageError) as info:
            parse_skill_md("# csv-tools\n설명만 있다")
        assert info.value.code == "frontmatter_missing"


class TestInspectPackage:
    def test_top_folder_layout(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(files={"scripts/run.py": "print('hi')\n", "docs/참고.md": "자료"})

        package = inspect_package(write(tmp_path, data))

        assert package.root == "csv-tools/"
        assert package.manifest.name == "csv-tools"
        assert package.manifest.description == "CSV 파일을 읽고 요약하는 도구"
        assert [f.path for f in package.manifest.files] == [
            "SKILL.md",
            "docs/참고.md",
            "scripts/run.py",
        ]
        assert package.skill_md.startswith("---\nname: csv-tools")

    def test_root_layout_and_dot_skill_name(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(top=None, files={"helper.py": "x = 1\n"})

        package = inspect_package(write(tmp_path, data, "csv-tools.skill"))

        assert package.root == ""
        assert {f.path for f in package.manifest.files} == {"SKILL.md", "helper.py"}

    def test_directory_entries_and_backslash_names(self, tmp_path: Path) -> None:
        # Windows PowerShell 5.1의 Compress-Archive는 구분자를 '\'로 쓴다.
        data = raw_zip(
            [
                ("my-skill/", b""),
                (zipfile.ZipInfo("my-skill\\SKILL.md"), front("name: my-skill\ndescription: d")),
                (zipfile.ZipInfo("my-skill\\scripts\\a.py"), "pass\n"),
            ]
        )

        package = inspect_package(write(tmp_path, data))

        assert package.root == "my-skill/"
        assert [f.path for f in package.manifest.files] == ["SKILL.md", "scripts/a.py"]

    def test_not_a_zip(self, tmp_path: Path) -> None:
        assert rejected(tmp_path, b"PK\x03\x04 broken").code == "package_invalid"

    def test_missing_skill_md(self, tmp_path: Path) -> None:
        data = raw_zip([("tool/readme.md", "x"), ("tool/sub/SKILL.md", front("name: a"))])

        assert rejected(tmp_path, data).code == "skill_md_missing"

    def test_two_top_folders(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = raw_zip(
            [
                ("csv-tools/SKILL.md", front("name: csv-tools\ndescription: d")),
                ("__MACOSX/csv-tools/._SKILL.md", b"\x00"),
            ]
        )

        error = rejected(tmp_path, data)

        assert error.code == "package_layout_invalid"
        assert "__MACOSX" in error.message

    def test_frontmatter_missing(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(skill_md="# csv-tools\n프런트매터가 없다\n")

        assert rejected(tmp_path, data).code == "frontmatter_missing"

    @pytest.mark.parametrize(
        "name",
        [
            "../evil.txt",
            "csv-tools/../../evil.txt",
            "/abs.txt",
            "csv-tools/./x.txt",
            "C:/windows.txt",
            "csv-tools/a:stream",
        ],
    )
    def test_path_escape(self, tmp_path: Path, name: str) -> None:
        data = raw_zip(
            [
                ("csv-tools/SKILL.md", front("name: csv-tools\ndescription: d")),
                (zipfile.ZipInfo(name), "x"),
            ]
        )

        assert rejected(tmp_path, data).code == "package_path_invalid"

    @pytest.mark.parametrize("name", ["CON", "nul.txt", "Com1.log", "trailing.", "space "])
    def test_windows_reserved_names(self, tmp_path: Path, skill_zip: SkillZip, name: str) -> None:
        data = skill_zip(files={f"scripts/{name}": "x"})

        assert rejected(tmp_path, data).code == "package_path_invalid"

    def test_case_insensitive_duplicates(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(files={"Data.csv": "a", "data.CSV": "b"})

        assert rejected(tmp_path, data).code == "package_path_invalid"

    def test_symlink(self, tmp_path: Path) -> None:
        link = zipfile.ZipInfo("csv-tools/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = raw_zip(
            [
                ("csv-tools/SKILL.md", front("name: csv-tools\ndescription: d")),
                (link, "/etc/passwd"),
            ]
        )

        assert rejected(tmp_path, data).code == "package_symlink"

    def test_unpacked_size_limit(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        # 0으로 채운 51MB는 압축하면 수십 KB라 업로드 크기로는 걸리지 않는다(zip 폭탄 방어).
        data = skill_zip(files={"big.bin": bytes(51 * 1024 * 1024)})

        error = rejected(tmp_path, data)

        assert len(data) < 1024 * 1024
        assert (error.code, error.status) == ("package_too_large", 413)

    def test_package_size_limit(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(files={"a.txt": "x" * 5000})

        error = rejected(tmp_path, data, max_package_bytes=100)

        assert (error.code, error.status) == ("package_too_large", 413)

    def test_file_count_limit(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = skill_zip(files={f"f{i}.txt": "x" for i in range(5)})

        assert rejected(tmp_path, data, max_files=5).code == "package_too_many_files"

    def test_skill_md_size_limit(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        body = front("name: csv-tools\ndescription: d") + "가" * MAX_SKILL_MD_BYTES

        assert rejected(tmp_path, skill_zip(skill_md=body)).code == "skill_md_too_large"

    def test_skill_md_at_limit(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        head = front("name: csv-tools\ndescription: d")
        body = head + "x" * (MAX_SKILL_MD_BYTES - len(head.encode()))

        package = inspect_package(write(tmp_path, skill_zip(skill_md=body)))

        assert len(package.skill_md.encode()) == MAX_SKILL_MD_BYTES

    def test_skill_md_must_be_utf8(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = raw_zip([("SKILL.md", front("name: a\ndescription: 설명").encode("cp949"))])

        assert rejected(tmp_path, data).code == "skill_md_invalid"

    def test_workspace_path_length(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        # skills/csv-tools/ 17자 + 이름 184자 = 201자. 작업공간 경로 한도는 200자다.
        data = skill_zip(files={"d" * 90 + "/" + "f" * 93: "x"})

        assert rejected(tmp_path, data).code == "package_path_too_long"

    def test_corrupt_member(self, tmp_path: Path, skill_zip: SkillZip) -> None:
        data = bytearray(skill_zip(files={"a.txt": "내용" * 100}))
        # 두 번째 항목의 압축 데이터 한가운데를 망가뜨린다.
        offset = data.rfind(b"PK\x03\x04") + 60
        data[offset : offset + 8] = b"\xff" * 8

        assert rejected(tmp_path, bytes(data)).code == "package_invalid"


def test_find_member_uses_the_skill_root(tmp_path: Path, skill_zip: SkillZip) -> None:
    path = write(tmp_path, skill_zip(files={"scripts/run.py": "print(1)\n"}))

    with zipfile.ZipFile(path) as archive:
        found = find_member(archive, "scripts/run.py")
        assert found is not None and found.filename == "csv-tools/scripts/run.py"
        assert find_member(archive, "../csv-tools/SKILL.md") is None
        assert find_member(archive, "csv-tools/SKILL.md") is None
        assert find_member(archive, "scripts") is None
