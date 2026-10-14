from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ApprovalMode = Literal["off", "http_writes", "all_shell"]


class RunLimits(BaseModel):
    # 기본값은 설계 4.6. 워크스페이스 기본 한도와 에이전트 버전의 limits가 같은 구조를 쓴다.
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=30, ge=1, le=200)
    max_run_seconds: int = Field(default=1800, ge=60, le=7200)
    max_total_tokens: int = Field(default=2_000_000, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    # None이면 200,000과 모델 컨텍스트의 60% 중 작은 값을 쓴다.
    context_budget_tokens: int | None = Field(default=None, ge=1)
    tool_timeout_sec: int = Field(default=120, ge=1, le=900)
    # 이 런이 call_agent로 만들 수 있는 자식 수. 뿌리 런의 값이 트리 전체 상한이고 자식 런에는
    # min(자식 설정, 뿌리의 남은 수)로 내려 준다(오케스트레이션 3.9). 0이면 call_agent를 붙이지
    # 않는다.
    max_child_calls: int = Field(default=20, ge=0, le=100)


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    # 없으면 입력 단가의 25%로 계산한다(설계 4.3).
    cached_input_per_mtok: float | None = Field(default=None, ge=0)
