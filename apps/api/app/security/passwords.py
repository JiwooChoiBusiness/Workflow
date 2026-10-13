from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 12
# argon2는 입력 길이에 비례해 일하므로 상한을 둔다.
MAX_PASSWORD_LENGTH = 256

# argon2-cffi 기본값(64MiB)은 해시 한 번에 메모리를 64MiB 쓴다. 플랫폼 메모리 예산이 300MB라
# OWASP 최소 권장값(19MiB, t=2, p=1)으로 둔다. 파라미터는 해시 문자열에 남으므로 나중에 올려도
# 기존 해시는 그대로 검증되고 로그인할 때 새 값으로 다시 해시한다(needs_rehash).
_hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    # 없는 이메일로 로그인해도 같은 시간만큼 검증해서, 응답 시간으로 계정 유무가 드러나지 않게 한다.
    return _hasher.hash("dummy-password-for-timing")
