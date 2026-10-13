import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ApprovalMode


class WebAppOut(BaseModel):
    """웹앱(/apps/<slug>)이 보여 주는 발행 에이전트 정보(6.3)."""

    agent_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    version_id: uuid.UUID
    version_number: int
    published_at: datetime | None
    # 발행 버전의 승인 모드. 화면이 채팅 헤더의 칩으로 보여 준다.
    approval_mode: ApprovalMode
