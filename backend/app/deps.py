"""FastAPI 依赖。讲解见 docs/08-fastapi-sse.md。

路由不直接去 app.state 里拿东西，而是通过 Depends 声明「我需要什么」。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from backend.store.base import SessionNotFound, StoredSession

from .access import current_visitor
from .chat_service import ChatService
from .schemas import ChatRequest


def get_service(request: Request) -> ChatService:
    return request.app.state.service


async def resolve_chat_session(
    body: ChatRequest, service: ChatService = Depends(get_service), visitor: str = Depends(current_visitor)
) -> StoredSession:
    """在流式响应开始之前确定会话。

    必须放在依赖里而不是生成器里：流式响应一旦开始，状态码 200 和响应头就已经发出去了，
    之后再发现会话不存在，已经没法返回 404。依赖在响应开始前执行，这里抛的异常会变成正常的 404。
    """
    try:
        return await service.open_session(visitor, body.session_id, body.message)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"会话不存在：{body.session_id}")
