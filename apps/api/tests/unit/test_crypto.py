import base64
import hashlib
import secrets

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.tokens import hash_token, new_api_key, new_session_token


def new_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher.from_base64(new_key())


def test_round_trip(cipher: SecretCipher) -> None:
    plaintext = "DB_PASSWORD=s3cr3t 한글 값"

    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


def test_format_is_v1_nonce_ciphertext_tag(cipher: SecretCipher) -> None:
    token = cipher.encrypt("value")

    version, nonce, ciphertext, tag = token.split("|")
    assert version == "v1"
    assert len(base64.b64decode(nonce)) == 12
    assert len(base64.b64decode(ciphertext)) == len(b"value")
    assert len(base64.b64decode(tag)) == 16
    assert "value" not in token


def test_same_plaintext_encrypts_differently(cipher: SecretCipher) -> None:
    assert cipher.encrypt("value") != cipher.encrypt("value")


def test_tampered_ciphertext_fails(cipher: SecretCipher) -> None:
    version, nonce, ciphertext, tag = cipher.encrypt("value").split("|")
    flipped = bytes([base64.b64decode(ciphertext)[0] ^ 1]) + base64.b64decode(ciphertext)[1:]
    tampered = "|".join([version, nonce, base64.b64encode(flipped).decode(), tag])

    with pytest.raises(CryptoError):
        cipher.decrypt(tampered)


def test_wrong_key_fails(cipher: SecretCipher) -> None:
    token = cipher.encrypt("value")

    with pytest.raises(CryptoError):
        SecretCipher.from_base64(new_key()).decrypt(token)


@pytest.mark.parametrize(
    "token", ["", "v1|a|b", "v2|AAAA|AAAA|AAAA", "v1|***|AAAA|AAAA", "v1|AAAA|AAAA|AAAA"]
)
def test_malformed_token_fails(cipher: SecretCipher, token: str) -> None:
    with pytest.raises(CryptoError):
        cipher.decrypt(token)


def test_bad_keys_are_rejected_without_echoing_them() -> None:
    short = base64.b64encode(b"x" * 16).decode()

    with pytest.raises(CryptoError) as not_base64:
        SecretCipher.from_base64("not base64 at all!")
    with pytest.raises(CryptoError) as wrong_length:
        SecretCipher.from_base64(short)

    assert "not base64" not in str(not_base64.value)
    assert short not in str(wrong_length.value)


def test_cipher_from_settings() -> None:
    with pytest.raises(CryptoError):
        cipher_from_settings(Settings.model_validate({}))

    configured = Settings.model_validate({"encryption_key": SecretStr(new_key())})
    cipher = cipher_from_settings(configured)
    assert cipher.decrypt(cipher.encrypt("x")) == "x"


def test_passwords_use_argon2id() -> None:
    hashed = hash_password("correct-horse-battery")

    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct-horse-battery")
    assert not verify_password(hashed, "correct-horse-batterY")
    assert not verify_password("not-a-hash", "correct-horse-battery")
    assert not needs_rehash(hashed)


def test_tokens() -> None:
    token = new_session_token()
    key = new_api_key()

    assert len(token) >= 43
    assert token != new_session_token()
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert key.raw.startswith("sk-wf-")
    assert key.prefix == key.raw[:12]
    assert key.hash == hash_token(key.raw)
