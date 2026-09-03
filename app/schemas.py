"""API 输入模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


class LoginInput(ApiModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=8, max_length=200)


class RegisterInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=50)
    confirm_password: str = Field(min_length=8, max_length=200)


class CommentInput(ApiModel):
    content: str = Field(min_length=1, max_length=1000)
    parent_id: int | None = None


class CategoryInput(ApiModel):
    name: str = Field(min_length=1, max_length=50)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,49}$")
    description: str = Field(default="", max_length=200)
    accent: str = Field(default="#8b7cff", pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: int = Field(default=10, ge=0, le=10000)
    is_active: bool = True


class GalleryInput(ApiModel):
    category_id: int
    title: str = Field(min_length=1, max_length=60)
    resource_dir: str = Field(min_length=1, max_length=500)
    status: str = Field(default="draft", pattern=r"^(draft|published|archived)$")
    is_featured: bool = False


class AnnouncementInput(ApiModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=5000)
    status: str = Field(default="published", pattern=r"^(published|archived)$")
    is_pinned: bool = False


class UserUpdateInput(ApiModel):
    display_name: str = Field(min_length=1, max_length=50)
    role: str = Field(pattern=r"^(admin|user)$")
    is_active: bool = True
    password: str | None = Field(default=None, min_length=8, max_length=200)


class UserCreateInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=50)
    role: str = Field(default="user", pattern=r"^(admin|user)$")


class SettingsInput(ApiModel):
    site_name: str = Field(min_length=1, max_length=50)
    site_tagline: str = Field(min_length=1, max_length=160)
    registration_enabled: bool
    login_per_minute: int = Field(ge=1, le=1000)
    register_per_hour: int = Field(ge=1, le=1000)
    comment_per_minute: int = Field(ge=1, le=1000)
