import hashlib
import secrets
from dataclasses import dataclass

SESSION_TOKEN_BYTES = 32
API_KEY_BYTES = 32
# sk- 접두는 로그 마스킹 패턴(app/logsetup.py)에 걸리게 하려는 것이다.
API_KEY_PREFIX = "sk-wf-"
API_KEY_DISPLAY_CHARS = 12


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


@dataclass(frozen=True)
class NewApiKey:
    raw: str
    prefix: str
    hash: str


def new_api_key() -> NewApiKey:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)
    return NewApiKey(raw=raw, prefix=raw[:API_KEY_DISPLAY_CHARS], hash=hash_token(raw))
