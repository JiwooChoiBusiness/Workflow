from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from app.llm.types import Usage

# 가격표에 캐시 입력 단가가 없으면 입력 단가의 25%로 본다(설계 4.3).
DEFAULT_CACHED_INPUT_RATIO = 0.25


class ModelPrice(BaseModel):
    """workspace.settings.model_prices[model_id] 한 항목. 단가는 100만 토큰당 USD."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)


def compute_cost(usage: Usage, price: ModelPrice) -> float:
    cached = min(usage.cached_input_tokens or 0, usage.input_tokens)
    cached_rate = price.cached_input_per_mtok
    if cached_rate is None:
        cached_rate = price.input_per_mtok * DEFAULT_CACHED_INPUT_RATIO
    total = (
        (usage.input_tokens - cached) * price.input_per_mtok
        + cached * cached_rate
        + usage.output_tokens * price.output_per_mtok
    )
    return total / 1_000_000


def cost_usd(
    model_id: str,
    usage: Usage,
    model_prices: Mapping[str, ModelPrice | Mapping[str, Any]] | None,
) -> float | None:
    """가격표에 그 모델이 있을 때만 비용을 계산하고, 없으면 None(토큰만 표시)."""
    if not model_prices:
        return None
    entry = model_prices.get(model_id)
    if entry is None:
        return None
    price = entry if isinstance(entry, ModelPrice) else ModelPrice.model_validate(entry)
    return compute_cost(usage, price)
