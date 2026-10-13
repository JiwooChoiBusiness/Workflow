import base64
import binascii
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings

VERSION = "v1"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16


class CryptoError(Exception):
    pass


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


class SecretCipher:
    # AES-256-GCM. 저장 형식은 v1|nonce|ciphertext|tag이고 각 조각은 base64다.
    # 앞의 v1은 나중에 알고리즘이나 키를 바꿀 때 옛 값을 구분하려고 둔다.

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError(f"ENCRYPTION_KEY는 {KEY_BYTES}바이트여야 한다(지금 {len(key)}바이트)")
        self._aead = AESGCM(key)

    @classmethod
    def from_base64(cls, value: str) -> "SecretCipher":
        try:
            key = _b64decode(value.strip())
        except (binascii.Error, UnicodeEncodeError):
            # 값 자체는 오류 메시지에 넣지 않는다.
            raise CryptoError("ENCRYPTION_KEY가 base64가 아니다") from None
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        ciphertext, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
        parts = (nonce, ciphertext, tag)
        return "|".join([VERSION, *(base64.b64encode(part).decode("ascii") for part in parts)])

    def decrypt(self, token: str) -> str:
        pieces = token.split("|")
        if len(pieces) != 4 or pieces[0] != VERSION:
            raise CryptoError("암호문 형식이 v1|nonce|ciphertext|tag가 아니다")
        try:
            nonce, ciphertext, tag = (_b64decode(piece) for piece in pieces[1:])
        except (binascii.Error, UnicodeEncodeError):
            raise CryptoError("암호문 조각이 base64가 아니다") from None
        if len(nonce) != NONCE_BYTES or len(tag) != TAG_BYTES:
            raise CryptoError("nonce 또는 tag 길이가 맞지 않다")
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext + tag, None)
        except InvalidTag:
            raise CryptoError("복호화 실패: 키가 다르거나 암호문이 바뀌었다") from None
        return plaintext.decode("utf-8")


def cipher_from_settings(settings: Settings) -> SecretCipher:
    if settings.encryption_key is None:
        raise CryptoError("ENCRYPTION_KEY가 비어 있다. .env.example의 안내대로 값을 넣는다")
    return SecretCipher.from_base64(settings.encryption_key.get_secret_value())
