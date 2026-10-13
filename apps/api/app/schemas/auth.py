import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from app.schemas.workspace import WorkspaceSummary
from app.security.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+$",
    ),
]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
NewPassword = Annotated[
    SecretStr, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
]
Password = Annotated[SecretStr, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class SetupStatus(BaseModel):
    needs_setup: bool
    password_min_length: int


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    password: NewPassword
    # 비우면 이메일의 @ 앞부분을 쓴다.
    name: DisplayName | None = None
    workspace_name: DisplayName | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    password: Password


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Password
    new_password: NewPassword


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    last_login_at: datetime | None
    created_at: datetime


class MeOut(BaseModel):
    account: AccountOut
    workspace: WorkspaceSummary
