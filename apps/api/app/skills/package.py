"""스킬 패키지 검증(설계 4.8).

업로드 때 한 번 검사하고 통과한 zip을 그대로 저장한다. 작업공간에 푸는 쪽
(app/sandbox/provisioning.py)과 같은 배치 규칙·경로 규칙을 써야 업로드에 통과한 패키지가 나중에
풀리지 않는 일이 없다.

SKILL.md 프런트매터는 YAML이지만 YAML 라이브러리를 의존성에 두지 않았으므로, 스킬 프런트매터에서
쓰는 범위(최상위 'key: value', 따옴표 문자열, 블록 문자열 |·>)만 읽는다. 중첩 매핑·목록은 값을
해석하지 않고 건너뛴다(name·description·version만 쓴다).
"""

import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.sandbox.base import SandboxError
from app.sandbox.paths import MAX_INPUT_LENGTH, split_relative
from app.schemas.skill import SkillManifest, SkillPackageFile

# zipfile은 업로드·내용 보기 때만 쓰므로 기동 메모리에 올리지 않는다(설계 2.7).
if TYPE_CHECKING:
    import zipfile

MAX_PACKAGE_BYTES = 50 * 1024 * 1024
# 압축을 푼 합계에도 같은 상한을 둔다. 작은 zip이 풀면서 디스크를 채우는 것(zip 폭탄)을 막는다.
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000
MAX_SKILL_MD_BYTES = 32 * 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_VERSION_CHARS = 64
SKILL_MD = "SKILL.md"
SKILL_NAME = re.compile(r"[a-z0-9-]{1,64}")
_ENCRYPTED_FLAG = 0x1


class SkillPackageError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class FrontmatterError(ValueError):
    pass


class FrontmatterMissing(FrontmatterError):
    pass


@dataclass(frozen=True)
class Structured:
    """중첩 매핑·목록·흐름 표기처럼 이 파서가 해석하지 않는 값. 원문만 들고 있다."""

    raw: str


FrontmatterValue = str | None | Structured


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    version: str | None


@dataclass(frozen=True)
class InspectedPackage:
    manifest: SkillManifest
    skill_md: str
    # SKILL.md가 있는 폴더. 루트면 '', 최상위 폴더 하나 안이면 '<폴더>/'.
    root: str


_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_KEY_LINE = re.compile(r"(?P<key>[^\s#:][^:]*?)[ \t]*:(?:[ \t]+(?P<value>.*?))?[ \t]*")
_BLOCK_HEADER = re.compile(r"(?P<style>[|>])(?P<flags>[1-9+-]{0,2})(?:[ \t]+#.*)?")
_NULLS = frozenset({"~", "null", "Null", "NULL"})
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": " ",
    "P": " ",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def split_frontmatter(text: str) -> tuple[list[str], str]:
    """(프런트매터 줄들, 본문)."""
    lines = _LINE_BREAK.split(text.removeprefix("﻿"))
    if not lines or lines[0].rstrip() != "---":
        raise FrontmatterMissing("SKILL.md 첫 줄이 '---'가 아니다")
    for index in range(1, len(lines)):
        if lines[index].rstrip() in ("---", "..."):
            return lines[1:index], "\n".join(lines[index + 1 :])
    raise FrontmatterError("프런트매터를 닫는 '---' 줄이 없다")


def parse_frontmatter(text: str) -> dict[str, FrontmatterValue]:
    lines, _ = split_frontmatter(text)
    values: dict[str, FrontmatterValue] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        # 파일 기준 줄 번호. 첫 줄이 '---'다.
        lineno = index + 2
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[0] in " \t":
            raise FrontmatterError(f"{lineno}번째 줄: 들여쓰기가 맞지 않다")
        match = _KEY_LINE.fullmatch(line)
        if match is None:
            raise FrontmatterError(f"{lineno}번째 줄: 'key: value' 형식이 아니다")
        key = match["key"]
        if key in values:
            raise FrontmatterError(f"{lineno}번째 줄: '{key}' 키가 두 번 나온다")
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
            end += 1
        values[key] = _value(match["value"] or "", lines[index + 1 : end], lineno)
        index = end
    return values


def _value(raw: str, block: list[str], lineno: int) -> FrontmatterValue:
    content = [line.strip() for line in block if line.strip()]
    if raw[:1] in ("|", ">"):
        return _block_scalar(raw, block, lineno)
    if raw[:1] in ('"', "'"):
        # 여러 줄에 걸친 따옴표 문자열은 줄바꿈이 공백 하나로 접힌다.
        return _quoted(" ".join([raw, *content]), lineno)
    if raw[:1] in ("[", "{", "&", "*", "!"):
        return Structured("\n".join([raw, *block]))
    if not raw or raw.startswith("#"):
        # 값 없이 들여쓴 줄이 따라오면 중첩 매핑·목록이다.
        return Structured("\n".join(block)) if content else None
    words = [_strip_comment(raw), *(_strip_comment(line) for line in content)]
    plain = " ".join(word for word in words if word)
    return None if plain in _NULLS else plain


def _strip_comment(text: str) -> str:
    match = re.search(r"[ \t]#", text)
    return (text[: match.start()] if match else text).strip()


def _block_scalar(header: str, block: list[str], lineno: int) -> str:
    match = _BLOCK_HEADER.fullmatch(header)
    if match is None:
        raise FrontmatterError(f"{lineno}번째 줄: 블록 문자열 표시가 올바르지 않다")
    flags = match["flags"]
    digits = [char for char in flags if char.isdigit()]
    first = next((line for line in block if line.strip()), None)
    if first is None:
        return ""
    indent = int(digits[0]) if digits else len(first) - len(first.lstrip(" "))
    if indent == 0:
        raise FrontmatterError(f"{lineno + 1}번째 줄: 블록 문자열 내용은 들여써야 한다")
    body: list[str] = []
    for offset, line in enumerate(block, start=1):
        if not line.strip():
            body.append("")
            continue
        if len(line) - len(line.lstrip(" ")) < indent:
            raise FrontmatterError(f"{lineno + offset}번째 줄: 블록 문자열 들여쓰기가 맞지 않다")
        body.append(line[indent:])
    trailing = 0
    while body and body[-1] == "":
        body.pop()
        trailing += 1
    if match["style"] == "|":
        text = "\n".join(body)
    else:
        # 접는 블록(>)은 빈 줄로 나뉜 문단 안의 줄바꿈을 공백으로 바꾼다.
        paragraphs: list[str] = []
        current: list[str] = []
        for line in body:
            if line:
                current.append(line)
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    if "-" in flags or not body:
        return text
    return text + "\n" * (1 + trailing if "+" in flags else 1)


def _quoted(text: str, lineno: int) -> str:
    quote = text[0]
    out: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if quote == "'" and char == "'":
            if text[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            break
        if quote == '"' and char == '"':
            break
        if quote == '"' and char == "\\":
            escape = text[index + 1 : index + 2]
            if escape in _HEX_ESCAPES:
                width = _HEX_ESCAPES[escape]
                digits = text[index + 2 : index + 2 + width]
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise FrontmatterError(
                        f"{lineno}번째 줄: \\{escape} 이스케이프가 올바르지 않다"
                    )
                out.append(chr(int(digits, 16)))
                index += 2 + width
                continue
            if escape not in _ESCAPES:
                raise FrontmatterError(f"{lineno}번째 줄: 지원하지 않는 이스케이프 \\{escape}")
            out.append(_ESCAPES[escape])
            index += 2
            continue
        out.append(char)
        index += 1
    else:
        raise FrontmatterError(f"{lineno}번째 줄: 따옴표가 닫히지 않았다")
    rest = text[index + 1 :]
    if rest.strip() and not re.match(r"[ \t]+#", rest):
        raise FrontmatterError(f"{lineno}번째 줄: 닫는 따옴표 뒤에 다른 내용이 있다")
    return "".join(out)


def parse_skill_md(text: str) -> SkillFrontmatter:
    try:
        values = parse_frontmatter(text)
    except FrontmatterMissing as exc:
        raise SkillPackageError(
            "frontmatter_missing",
            f"{exc}. SKILL.md는 '---'로 감싼 프런트매터(name, description)로 시작해야 한다",
        ) from exc
    except FrontmatterError as exc:
        raise SkillPackageError(
            "frontmatter_invalid", f"SKILL.md 프런트매터를 읽지 못했다: {exc}"
        ) from exc

    name = values.get("name")
    if not isinstance(name, str):
        raise SkillPackageError("skill_name_invalid", "프런트매터에 name이 없다")
    if not SKILL_NAME.fullmatch(name):
        raise SkillPackageError(
            "skill_name_invalid",
            f"name은 영문 소문자·숫자·하이픈으로 된 1~64자여야 한다: {name[:80]!r}",
        )
    description = values.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillPackageError("skill_description_invalid", "프런트매터에 description이 없다")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillPackageError(
            "skill_description_invalid",
            f"description은 {MAX_DESCRIPTION_CHARS}자 이하여야 한다(지금 {len(description)}자)",
        )
    version = values.get("version")
    if isinstance(version, Structured) or (
        isinstance(version, str) and len(version.strip()) > MAX_VERSION_CHARS
    ):
        raise SkillPackageError(
            "skill_version_invalid", f"version은 {MAX_VERSION_CHARS}자 이하의 문자열이어야 한다"
        )
    return SkillFrontmatter(
        name=name, description=description, version=version.strip() if version else None
    )


def entry_name(info: "zipfile.ZipInfo") -> tuple[str, bool]:
    """(구분자를 '/'로 맞춘 이름, 디렉터리 여부).

    Windows PowerShell 5.1의 Compress-Archive는 구분자를 '\\'로 쓴다. Windows의 zipfile도 읽을 때
    같은 변환을 하므로 작업공간에 풀 때와 이름이 같다.
    """
    name = info.filename.replace("\\", "/")
    is_dir = name.endswith("/")
    return (name[:-1] if is_dir else name), is_dir


def package_root(names: Iterable[str]) -> str:
    """SKILL.md가 있는 폴더. provisioning._skill_layout과 같은 규칙이다."""
    files = list(names)
    if SKILL_MD in files:
        return ""
    tops = sorted({name.split("/", 1)[0] for name in files})
    if len(tops) == 1 and f"{tops[0]}/{SKILL_MD}" in files:
        return f"{tops[0]}/"
    if len(tops) > 1 and any(name.endswith(f"/{SKILL_MD}") for name in files):
        hint = " macOS의 __MACOSX 폴더는 빼고 압축한다." if "__MACOSX" in tops else ""
        shown = ", ".join(tops[:5])
        raise SkillPackageError(
            "package_layout_invalid",
            f"최상위에 폴더·파일이 여러 개다({shown}). 폴더 하나에 SKILL.md와 자료를 넣거나 "
            f"SKILL.md를 루트에 둔다.{hint}",
        )
    raise SkillPackageError(
        "skill_md_missing", "SKILL.md가 없다. 루트나 최상위 폴더 바로 아래에 SKILL.md를 둔다"
    )


def _check_entry_name(rel: str, original: str) -> None:
    if rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        raise SkillPackageError(
            "package_path_invalid", f"패키지 밖을 가리키는 항목: {original[:80]!r}"
        )
    try:
        # 절대 경로·드라이브·금지 문자·Windows 예약 이름을 작업공간 경로 규칙(3.5)으로 거른다.
        split_relative(rel, os_name="nt")
    except SandboxError as exc:
        raise SkillPackageError(
            "package_path_invalid", f"쓸 수 없는 항목 이름: {exc.message}"
        ) from exc


def _package_files(
    infos: list["zipfile.ZipInfo"], *, max_files: int, max_unpacked_bytes: int
) -> list[tuple[str, "zipfile.ZipInfo"]]:
    # 디렉터리 항목까지 세어도 터무니없이 많으면 이름 검사 전에 끊는다.
    if len(infos) > max_files * 2:
        raise SkillPackageError("package_too_many_files", f"파일이 {max_files}개를 넘는다")
    files: list[tuple[str, zipfile.ZipInfo]] = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        rel, is_dir = entry_name(info)
        if info.flag_bits & _ENCRYPTED_FLAG:
            raise SkillPackageError("package_invalid", f"암호가 걸린 항목: {info.filename[:80]!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise SkillPackageError(
                "package_symlink", f"심볼릭 링크는 넣을 수 없다: {info.filename[:80]!r}"
            )
        _check_entry_name(rel, info.filename)
        # Windows는 대소문자를 가리지 않아 둘 중 하나가 다른 하나를 덮어쓴다.
        folded = rel.casefold()
        if folded in seen:
            raise SkillPackageError(
                "package_path_invalid", f"같은 이름이 두 번 나온다: {info.filename[:80]!r}"
            )
        seen.add(folded)
        if is_dir:
            continue
        files.append((rel, info))
        total += info.file_size
    if len(files) > max_files:
        raise SkillPackageError(
            "package_too_many_files", f"파일이 {max_files}개를 넘는다(지금 {len(files)}개)"
        )
    if total > max_unpacked_bytes:
        raise SkillPackageError(
            "package_too_large",
            f"압축을 풀면 {max_unpacked_bytes // 2**20}MB를 넘는다({total / 2**20:.1f}MB)",
            status=413,
        )
    return files


def _read_skill_md(archive: "zipfile.ZipFile", info: "zipfile.ZipInfo") -> str:
    too_large = SkillPackageError(
        "skill_md_too_large",
        f"SKILL.md는 {MAX_SKILL_MD_BYTES // 1024}KB 이하여야 한다(지금 {info.file_size}바이트). "
        "긴 내용은 다른 파일로 나누고 SKILL.md에서 그 경로를 안내한다",
    )
    if info.file_size > MAX_SKILL_MD_BYTES:
        raise too_large
    with archive.open(info) as source:
        data = source.read(MAX_SKILL_MD_BYTES + 1)
    if len(data) > MAX_SKILL_MD_BYTES:
        raise too_large
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SkillPackageError("skill_md_invalid", "SKILL.md가 UTF-8 텍스트가 아니다") from None


def inspect_package(
    path: Path,
    *,
    max_package_bytes: int = MAX_PACKAGE_BYTES,
    max_unpacked_bytes: int = MAX_UNPACKED_BYTES,
    max_files: int = MAX_FILES,
) -> InspectedPackage:
    """zip(.zip, .skill)을 검사해 manifest와 SKILL.md를 돌려준다. 동기 함수라 스레드에서 부른다."""
    import zipfile
    import zlib

    size = path.stat().st_size
    if size > max_package_bytes:
        raise SkillPackageError(
            "package_too_large",
            f"스킬 패키지는 {max_package_bytes // 2**20}MB 이하여야 한다",
            status=413,
        )
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise SkillPackageError("package_invalid", "zip 파일이 아니다") from None
    with archive:
        files = _package_files(
            archive.infolist(), max_files=max_files, max_unpacked_bytes=max_unpacked_bytes
        )
        root = package_root(name for name, _ in files)
        skill_md = _read_skill_md(archive, dict(files)[root + SKILL_MD])
        frontmatter = parse_skill_md(skill_md)
        package_files: list[SkillPackageFile] = []
        for name, info in files:
            rel = name[len(root) :]
            # 작업공간에서는 skills/<name>/<rel>로 풀린다. 경로 검사(3.5)의 길이 한도에 걸리면
            # 런이 시작될 때가 아니라 지금 알려 준다.
            workspace_path = f"skills/{frontmatter.name}/{rel}"
            if len(workspace_path) > MAX_INPUT_LENGTH:
                raise SkillPackageError(
                    "package_path_too_long",
                    f"경로가 너무 길다({len(workspace_path)}자, 한도 {MAX_INPUT_LENGTH}자): "
                    f"{rel[:80]!r}",
                )
            package_files.append(SkillPackageFile(path=rel, size=info.file_size))
        try:
            corrupt = archive.testzip()
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, NotImplementedError) as exc:
            raise SkillPackageError("package_invalid", f"압축을 풀 수 없다: {exc}") from None
        if corrupt is not None:
            raise SkillPackageError("package_invalid", f"손상된 항목: {corrupt[:80]!r}")
    manifest = SkillManifest(
        name=frontmatter.name,
        description=frontmatter.description,
        version=frontmatter.version,
        files=sorted(package_files, key=lambda item: item.path),
    )
    return InspectedPackage(manifest=manifest, skill_md=skill_md, root=root)


def find_member(archive: "zipfile.ZipFile", rel: str) -> "zipfile.ZipInfo | None":
    """스킬 루트 기준 상대 경로 rel의 zip 항목. 이름을 정확히 비교하므로 경로 탈출이 없다."""
    files: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name, is_dir = entry_name(info)
        if not is_dir:
            files[name] = info
    try:
        root = package_root(files)
    except SkillPackageError:
        return None
    return files.get(root + rel)
