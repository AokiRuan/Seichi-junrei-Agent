"""HTTP 接口的请求 / 响应模型。讲解见 docs/08-fastapi-sse.md。

这些模型同时决定了 /docs 页面上自动生成的接口文档。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from backend.store.base import StoredMessage


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, description="用户输入")
    session_id: Optional[str] = Field(default=None, description="会话 id；不传则新建会话")


class AccessRequest(BaseModel):
    code: str = Field(max_length=200, description="访问码")


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionOut):
    messages: list[StoredMessage]
