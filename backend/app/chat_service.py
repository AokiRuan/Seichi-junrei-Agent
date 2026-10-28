"""一轮对话的完整流程：载入历史 → 运行 agent → 持久化。讲解见 docs/08-fastapi-sse.md。

这一层介于 HTTP 路由与 agent loop 之间：
- loop 只负责「回答」，不知道会话、数据库的存在（依赖方向，见 docs/01 原则一）
- 路由只负责「把事件编码成 SSE」，不关心历史怎么载入、结果怎么保存
- 中间的「会话怎么接到 loop 上」放在这里。CLI 与 HTTP 共用它
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from backend.agent.events import AgentEvent
from backend.agent.prompts import build_system_prompt
from backend.store.base import SessionNotFound, StoredMessage, StoredSession

from .components import Components

TITLE_MAX_CHARS = 30

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, components: Components) -> None:
        self._c = components

    @property
    def store(self):
        return self._c.store

    # ---- 会话归属：所有按 id 读会话的入口都经过 _owned，HTTP 与 CLI 一致。讲解见 docs/12-going-public.md

    async def _owned(self, owner: str, session_id: str) -> StoredSession:
        """取属于 owner 的会话。别人的会话和不存在的会话一样抛 SessionNotFound（对外都是 404）：
        返回 403 等于告诉对方「这个 id 存在」。"""
        session = await self._c.store.get_session(session_id)
        if session is None or session.owner != owner:
            raise SessionNotFound(session_id)
        return session

    async def open_session(self, owner: str, session_id: str | None, first_message: str) -> StoredSession:
        """取已有会话；session_id 为空时新建。不存在或不属于 owner 时抛 SessionNotFound。"""
        if session_id:
            return await self._owned(owner, session_id)
        return await self._c.store.create_session(_make_title(first_message), owner)

    async def list_sessions(self, owner: str) -> list[StoredSession]:
        return await self._c.store.list_sessions(owner)

    async def get_history(self, owner: str, session_id: str) -> tuple[StoredSession, list[StoredMessage]]:
        session = await self._owned(owner, session_id)
        return session, await self._c.store.get_messages(session_id)

    async def run_turn(self, session: StoredSession, user_text: str) -> AsyncIterator[AgentEvent]:
        history = await self._c.store.get_messages(session.id)
        user_msg = StoredMessage(role="user", content=user_text)
        # system prompt 每轮重新生成（里面有当前日期），不存进会话
        messages = [
            {"role": "system", "content": build_system_prompt()},
            *(m.to_llm_message() for m in history),
            user_msg.to_llm_message(),
        ]

        tool_names: dict[str, str] = {}
        tool_results: dict[str, dict[str, Any]] = {}

        with self._c.tracing.turn(session_id=session.id, user_input=user_text) as turn:
            async for event in self._c.loop.run(messages):
                if event.type == "tool_call":
                    tool_names[event.tool_call_id] = event.tool_name
                elif event.type == "tool_result":
                    tool_results[event.tool_call_id] = event.payload
                elif event.type == "done":
                    new = [_to_stored(m, tool_names, tool_results) for m in event.payload["new_messages"]]
                    # 先存再发 done：前端收到 done 时，刷新会话列表一定能看到这一轮
                    await self._c.store.append_messages(session.id, [user_msg, *new])
                    turn.finish(
                        _final_answer(new),
                        iterations=event.payload["iterations"],
                        usage=event.payload["usage"],
                    )
                elif event.type == "error":
                    # 出错时本轮不入库：存半截的历史（比如有工具调用、没有回答）
                    # 下次再发给 LLM 反而可能出问题。用户可以重新提问
                    turn.fail(event.message or "")
                    # 错误事件只发给了客户端；不记日志的话，服务端完全不知道出过错
                    logger.warning("session=%s turn failed: %s", session.id, event.message)
                yield event


def _make_title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX_CHARS else text[:TITLE_MAX_CHARS] + "…"


def _to_stored(msg: dict[str, Any], names: dict[str, str], results: dict[str, dict[str, Any]]) -> StoredMessage:
    """loop 产出的 OpenAI 格式消息 → StoredMessage。tool 消息补上结构化结果，供前端恢复地图打点。"""
    stored = StoredMessage(
        role=msg["role"],
        content=msg.get("content"),
        tool_calls=msg.get("tool_calls"),
        tool_call_id=msg.get("tool_call_id"),
    )
    if stored.role == "tool" and stored.tool_call_id:
        result = results.get(stored.tool_call_id, {})
        stored.tool_name = names.get(stored.tool_call_id)
        stored.is_error = result.get("is_error")
        stored.data = result.get("data")
    return stored


def _final_answer(messages: list[StoredMessage]) -> str:
    for m in reversed(messages):
        if m.role == "assistant" and m.content:
            return m.content
    return ""
