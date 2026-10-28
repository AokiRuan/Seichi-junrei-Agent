"""会话读取接口。讲解见 docs/08-fastapi-sse.md。

GET /api/sessions        —— 当前访客的会话列表（按最近更新倒序），供前端侧栏渲染
GET /api/sessions/{id}   —— 完整消息，供刷新页面后恢复，包含工具调用记录与地图打点数据

只能看到自己（访客 cookie）的会话，见 docs/12-going-public.md
"""
from fastapi import APIRouter, Depends, HTTPException

from backend.store.base import SessionNotFound

from ..access import current_visitor
from ..chat_service import ChatService
from ..deps import get_service
from ..schemas import SessionDetail, SessionOut

router = APIRouter(prefix="/sessions")


@router.get("", response_model=list[SessionOut])
async def list_sessions(service: ChatService = Depends(get_service), visitor: str = Depends(current_visitor)):
    return await service.list_sessions(visitor)


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str, service: ChatService = Depends(get_service), visitor: str = Depends(current_visitor)
):
    try:
        session, messages = await service.get_history(visitor, session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return SessionDetail(**session.model_dump(), messages=messages)
