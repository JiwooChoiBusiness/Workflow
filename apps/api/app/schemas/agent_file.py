import uuid
from datetime import datetime

from pydantic import BaseModel


class AgentFileOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    # 작업공간에서는 agent-files/<filename>으로 복사된다.
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    created_at: datetime
    # 초안의 file_ids에 있는지. 업로드만으로는 초안에 들어가지 않는다(PUT .../draft로 넣는다).
    in_draft: bool
    # 이 파일을 쓰는 발행 버전 번호. 비어 있지 않으면 지울 수 없다.
    published_versions: list[int]


class AgentFileList(BaseModel):
    items: list[AgentFileOut]
