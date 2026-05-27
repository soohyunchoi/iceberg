"""Pydantic v2 request/response schemas and shared enums."""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class MatchType(str, Enum):
    AUTO_LINKED = "AUTO_LINKED"
    USER_CONFIRMED = "USER_CONFIRMED"
    NEW_CANONICAL = "NEW_CANONICAL"


def _sanitize(v: str) -> str:
    v = _CONTROL_CHARS.sub("", v)
    v = v.strip()
    if len(v) == 0:
        raise ValueError("empty after sanitization")
    return v


class ThoughtInput(BaseModel):
    text: str = Field(min_length=1, max_length=280)

    @field_validator("text")
    @classmethod
    def sanitize(cls, v: str) -> str:
        return _sanitize(v)


class ConfirmInput(BaseModel):
    """Grey-zone follow-up: user picked one of the presented candidates."""

    text: str = Field(min_length=1, max_length=280)
    canonical_id: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def sanitize(cls, v: str) -> str:
        return _sanitize(v)


class Candidate(BaseModel):
    canonical_id: str
    text: str
    score: float


class SignupInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class RefreshInput(BaseModel):
    refresh_token: str = Field(min_length=1)
