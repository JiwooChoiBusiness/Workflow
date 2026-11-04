"""지식 베이스 콘솔 API(3단계 설계 12.1): 목록·생성·설정·삭제, 통계·기본값, 메타데이터 필드."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request

from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseList,
    KnowledgeBaseOut,
    KnowledgeBasePatch,
    KnowledgeBaseStats,
    KnowledgeDefaults,
    MetadataFieldCreate,
    MetadataFieldList,
    MetadataFieldOut,
    MetadataFieldPatch,
)
from app.services.knowledge import KnowledgeService
from app.services.knowledge_metadata import KnowledgeMetadataService
from app.services.knowledge_runtime import KnowledgeRuntime, index_unavailable

router = APIRouter(tags=["knowledge"])


def get_knowledge_service(request: Request, db: DbDep, settings: SettingsDep) -> KnowledgeService:
    # KB를 지우면 그 문서들의 대기·실행 중 작업을 레인에서 뺀다.
    return KnowledgeService(db, settings, lane=getattr(request.app.state, "knowledge_lane", None))


def _runtime(request: Request) -> KnowledgeRuntime | None:
    # 통계·기본값은 색인 파일을 못 써도 503이 아니라 app.db 쪽 값과 null로 답한다.
    runtime = getattr(request.app.state, "knowledge", None)
    return runtime if isinstance(runtime, KnowledgeRuntime) else None


def get_metadata_service(db: DbDep) -> KnowledgeMetadataService:
    return KnowledgeMetadataService(db)


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]
MetadataServiceDep = Annotated[KnowledgeMetadataService, Depends(get_metadata_service)]


@router.get("/knowledge", response_model=KnowledgeBaseList)
async def list_knowledge_bases(
    auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeBaseList:
    """살아 있는 지식 베이스. 문서·청크 수와 대기·색인 중인 문서 수가 붙는다."""
    return await service.list_bases(auth.workspace_id)


@router.get("/knowledge-defaults", response_model=KnowledgeDefaults)
async def knowledge_defaults(
    request: Request, auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeDefaults:
    """임베딩 모델·차원 선택지, 새 KB의 기본 규칙과 검색 설정, 지금 돌고 있는 임베딩 리미터 값과
    오늘 사용량, 유료 키 유무, SQLite 버전과 FTS 모드, 색인 파일을 쓸 수 있는지, 업로드·검색 한도.

    색인 파일을 열지 못했어도 200이고 index_available이 false다.
    """
    runtime = _runtime(request)
    if runtime is None:
        # 지식 실행 객체는 lifespan이 늘 만든다. 없으면 리미터 값도 없어 답할 수 없다.
        raise index_unavailable()
    return service.defaults(runtime)


@router.post("/knowledge", status_code=201, response_model=KnowledgeBaseOut)
async def create_knowledge_base(
    body: KnowledgeBaseCreate, auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeBaseOut:
    """data_policy.acknowledged가 참이어야 만든다. key_tier=paid는 GEMINI_PAID_API_KEY가 있어야 하고
    없으면 422 paid_key_missing이다. doc_form은 만든 뒤 바꿀 수 없다.
    """
    return await service.create_base(auth.workspace_id, body)


@router.get("/knowledge/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: uuid.UUID, auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeBaseOut:
    return await service.get_base(auth.workspace_id, kb_id)


@router.patch("/knowledge/{kb_id}", response_model=KnowledgeBaseOut)
async def patch_knowledge_base(
    kb_id: uuid.UUID, body: KnowledgeBasePatch, auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeBaseOut:
    """embedding을 바꾸면 confirm_reembed가 참이어야 하고(아니면 409 reembed_confirmation_required)
    모든 문서가 waiting으로 돌아가 벡터를 다시 만든다. economy → high_quality도 같다. 반대 방향은
    422 downgrade_not_allowed다.
    """
    return await service.update_base(auth.workspace_id, kb_id, body)


@router.get("/knowledge/{kb_id}/stats", response_model=KnowledgeBaseStats)
async def knowledge_stats(
    kb_id: uuid.UUID, request: Request, auth: CurrentAuth, service: KnowledgeServiceDep
) -> KnowledgeBaseStats:
    """문서 수(상태별)·청크·유닛 수, 벡터 완료율과 예상 남은 분(일일 한도 제외), index.db 크기,
    추정 임베딩 토큰, 일일 한도로 멈춘 문서 수와 가장 이른 retry_after. 색인 파일을 못 쓰면 색인
    값만 null이다."""
    return await service.stats(auth.workspace_id, kb_id, _runtime(request))


@router.delete("/knowledge/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: uuid.UUID, auth: CurrentAuth, service: KnowledgeServiceDep
) -> None:
    """소프트 삭제. 문서와 색인은 뒤에서 정리되고 이 KB의 API 키는 바로 폐기된다."""
    await service.delete_base(auth.workspace_id, kb_id)


@router.get("/knowledge/{kb_id}/metadata", response_model=MetadataFieldList)
async def list_metadata_fields(
    kb_id: uuid.UUID, auth: CurrentAuth, service: MetadataServiceDep
) -> MetadataFieldList:
    return await service.list_fields(auth.workspace_id, kb_id)


@router.post("/knowledge/{kb_id}/metadata", status_code=201, response_model=MetadataFieldOut)
async def create_metadata_field(
    kb_id: uuid.UUID, body: MetadataFieldCreate, auth: CurrentAuth, service: MetadataServiceDep
) -> MetadataFieldOut:
    """이름은 [a-z][a-z0-9_]* 40자 이하이고 기본 필드 이름은 쓸 수 없다. 타입은 바꿀 수 없다."""
    return await service.create_field(auth.workspace_id, kb_id, body)


@router.patch("/knowledge/{kb_id}/metadata/{field_id}", response_model=MetadataFieldOut)
async def rename_metadata_field(
    kb_id: uuid.UUID,
    field_id: uuid.UUID,
    body: MetadataFieldPatch,
    auth: CurrentAuth,
    service: MetadataServiceDep,
) -> MetadataFieldOut:
    return await service.rename_field(auth.workspace_id, kb_id, field_id, body)


@router.delete("/knowledge/{kb_id}/metadata/{field_id}", status_code=204)
async def delete_metadata_field(
    kb_id: uuid.UUID, field_id: uuid.UUID, auth: CurrentAuth, service: MetadataServiceDep
) -> None:
    """필드와 문서들의 그 값을 함께 지운다."""
    await service.delete_field(auth.workspace_id, kb_id, field_id)


@router.post("/knowledge/{kb_id}/metadata/built-in/{action}", response_model=MetadataFieldList)
async def toggle_built_in_fields(
    kb_id: uuid.UUID,
    action: Literal["enable", "disable"],
    auth: CurrentAuth,
    service: MetadataServiceDep,
) -> MetadataFieldList:
    """기본 필드(document_name, uploader, upload_date, last_update_date, source)를 켜면 만들고
    지금 문서들의 값을 채운다. 끄면 필드와 값을 지운다.
    """
    return await service.set_built_in(
        auth.workspace_id, kb_id, action == "enable", uploader=auth.account.name
    )
