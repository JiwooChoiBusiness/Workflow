"""에이전트 기록을 읽을 때 비밀값 가리기(3단계 KS9).

런은 메시지 행의 tool_use 입력과 256KB를 넘어 흘려 쓴 도구 출력 파일을 원문으로 남긴다. 입력은
승인 뒤 재개할 때 그대로 실행해야 하고, 파일은 출력이 흐르는 동안 쓴다. 그래서 메시지 기록 API와
도구 출력 내려받기가 내보낼 때 그 세션·런 버전의 비밀 환경변수(그때 복호화)와 설정 키로 가린다.
"""

import codecs
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Collection, Sequence
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories.agent_versions import AgentVersionRepository
from app.schemas.agent_version import StoredEnvVar
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.workflow.variables import Masker, StreamMasker

log = structlog.get_logger("app.services.secret_masking")


def version_secret_values(
    items: Sequence[StoredEnvVar], cipher: SecretCipher | None
) -> tuple[list[str], int]:
    """(복호화한 비밀값, 못 읽은 비밀값 수). 비밀이 아닌 항목은 보지 않는다."""
    values: list[str] = []
    unreadable = 0
    for item in items:
        if not item.secret:
            continue
        if cipher is None:
            unreadable += 1
            continue
        try:
            values.append(cipher.decrypt(item.value_encrypted))
        except CryptoError:
            unreadable += 1
    return values, unreadable


async def read_secret_values(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    version_ids: Collection[uuid.UUID],
    settings: Settings,
) -> list[str]:
    """에이전트 버전들의 비밀 환경변수와 설정 키.

    읽기 경로라 ENCRYPTION_KEY가 없거나 바뀌어 복호화하지 못한 값은 500으로 죽지 않고 건너뛴다(그
    값은 가려지지 않는다). 캐시하지 않아 초안의 환경변수를 고치면 바로 반영된다.
    """
    versions = await AgentVersionRepository(session, workspace_id).get_many(version_ids)
    try:
        cipher: SecretCipher | None = cipher_from_settings(settings)
    except CryptoError:
        cipher = None
    values: list[str] = []
    for version in versions:
        items: list[StoredEnvVar] = []
        for raw in version.env_vars or []:
            try:
                items.append(StoredEnvVar.model_validate(raw))
            except ValidationError:
                log.warning("agent.env_var_invalid")
        found, unreadable = version_secret_values(items, cipher)
        if unreadable:
            log.warning("run.read_mask_unreadable", version_id=str(version.id), count=unreadable)
        values.extend(found)
    return [*values, *settings.secret_values()]


def mask_blocks(
    masker: Masker, blocks: list[dict[str, Any]], *, tool_inputs: bool
) -> list[dict[str, Any]]:
    # 블록을 잇는 tool_use의 id·name과 tool_result의 tool_use_id는 두고 값만 바꾼다.
    if not masker.secrets:
        return blocks
    masked: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            block = {**block, "text": masker.text(block["text"])}
        elif kind == "tool_result" and isinstance(block.get("content"), str):
            block = {**block, "content": masker.text(block["content"])}
        elif kind == "tool_use" and tool_inputs and "input" in block:
            block = {**block, "input": masker.mask(block["input"])}
        masked.append(block)
    return masked


async def masked_chunks(chunks: AsyncIterable[bytes], masker: Masker) -> AsyncIterator[bytes]:
    """UTF-8 바이트 스트림을 가려 낸다. 비밀값이 없으면 받은 바이트를 손대지 않고 넘긴다.

    조각 경계에 걸친 비밀값은 StreamMasker가 끝부분을 붙잡아 다음 조각과 잇고, 경계에서 갈린
    멀티바이트 글자는 증분 디코더가 잇는다.
    """
    if not masker.secrets:
        async for chunk in chunks:
            yield chunk
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stream = StreamMasker(masker)
    async for chunk in chunks:
        text = stream.feed(decoder.decode(chunk))
        if text:
            yield text.encode("utf-8")
    rest = stream.feed(decoder.decode(b"", final=True)) + stream.flush()
    if rest:
        yield rest.encode("utf-8")
