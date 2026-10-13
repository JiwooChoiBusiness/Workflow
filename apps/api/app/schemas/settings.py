from datetime import datetime

from pydantic import BaseModel


class HttpAllowlistRules(BaseModel):
    # 그 호스트만(IP 주소 포함).
    hosts: list[str]
    # .으로 시작하는 항목. 하위 도메인만 허용하고 그 도메인 자신은 포함하지 않는다.
    suffixes: list[str]
    # * 한 줄이 있다. 모든 공개 호스트를 허용한다(IP 주소와 사설·루프백 주소는 여전히 막힌다).
    allow_any: bool


class HttpAllowlistOut(BaseModel):
    """http_request 허용 목록 파일(설계 3.9). 화면에서는 보기만 하고 고치려면 파일을 편집한다."""

    path: str
    exists: bool
    size_bytes: int | None
    modified_at: datetime | None
    # 파일 내용(앞 256KB까지). 넘으면 truncated.
    content: str
    truncated: bool
    rules: HttpAllowlistRules
    # 파일을 저장한 뒤 서버가 다시 읽기까지 걸리는 최대 시간(초). 서버를 다시 띄울 필요는 없다.
    reload_interval_sec: float
