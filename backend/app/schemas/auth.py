"""Request schemas. ``extra="forbid"`` rejects unknown fields; lengths cap hashing/DB work."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Generous caps; the real password policy lives in app.auth.passwords.
_PW = Field(min_length=1, max_length=1100)
Role = Literal["admin", "user", "read_only"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupRequest(_Strict):
    setup_token: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=1, max_length=64)
    password: str = _PW
    app_url: str | None = Field(default=None, max_length=300)


class LoginRequest(_Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = _PW
    code: str | None = Field(default=None, max_length=32)


class MfaConfirmRequest(_Strict):
    code: str = Field(min_length=1, max_length=32)


class MfaBeginRequest(_Strict):
    password: str = _PW


class MfaDisableRequest(_Strict):
    password: str = _PW
    code: str = Field(min_length=1, max_length=32)


class ChangePasswordRequest(_Strict):
    current_password: str = _PW
    new_password: str = _PW


class ResetRedeemRequest(_Strict):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = _PW


class CreateUserRequest(_Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = _PW
    role: Role = "user"


class UpdateUserRequest(_Strict):
    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateUserRequest:
        if self.role is None and self.is_active is None:
            raise ValueError("provide role and/or is_active")
        return self
