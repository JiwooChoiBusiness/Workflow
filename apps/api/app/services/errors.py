from typing import Any


class ServiceError(Exception):
    """서비스 계층의 실패. HTTP를 모르는 호출자(러너, 2단계 엔진)도 같은 예외를 받는다.

    API 계층은 이 예외를 {error: {code, message, details}} 응답으로 바꾼다(app/api/errors.py).
    """

    def __init__(self, status: int, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str) -> ServiceError:
    return ServiceError(404, "not_found", f"{what}을(를) 찾을 수 없다")


class Problems:
    """요청 값 검사 결과를 모아 한 번에 422로 돌려준다.

    details는 요청 본문 검증 오류(RequestValidationError)와 같은 {loc, msg, type} 모양이라 화면이
    필드별 오류를 한 가지 방식으로 표시할 수 있다. type은 오류 종류를 가리키는 고정 문자열이다.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, loc: tuple[str | int, ...], type_: str, msg: str, **ctx: Any) -> None:
        item: dict[str, Any] = {"loc": ["body", *loc], "msg": msg, "type": type_}
        if ctx:
            item["ctx"] = ctx
        self.items.append(item)

    def raise_if_any(self) -> None:
        if self.items:
            raise ServiceError(
                422, "validation_error", "요청 값이 올바르지 않다", details=self.items
            )
