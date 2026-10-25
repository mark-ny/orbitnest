"""Workspace request schemas (strict: unknown fields rejected; sizes capped)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkspaceRequest(_Strict):
    name: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)
    profile: str | None = Field(default=None, max_length=16)
    persistence: str | None = Field(default=None, max_length=16)
    network: str | None = Field(default=None, max_length=24)
    idle_timeout_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 14)
    labels: dict[str, str] | None = Field(default=None, max_length=10)
    acknowledge_authorized_use: bool = False


class UpdateWorkspaceRequest(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    labels: dict[str, str] | None = Field(default=None, max_length=10)
    idle_timeout_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 14)

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateWorkspaceRequest:
        if self.name is None and self.labels is None and self.idle_timeout_minutes is None:
            raise ValueError("provide at least one field to change")
        return self


class ResetWorkspaceRequest(_Strict):
    wipe_data: bool = False
    confirm_name: str | None = Field(default=None, max_length=64)


class CustomTemplateRequest(BaseModel):
    """The definition is validated by ``templates.validate_custom_definition``."""

    model_config = ConfigDict(extra="allow")


class TemplateEnabledRequest(_Strict):
    enabled: bool


class PolicyRequest(_Strict):
    max_workspaces_per_user: int | None = None
    max_running_per_user: int | None = None
    idle_mark_minutes: int | None = None
    idle_action: str | None = Field(default=None, max_length=8)

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}
