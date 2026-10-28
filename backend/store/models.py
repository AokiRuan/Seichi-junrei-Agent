"""SQLAlchemy 模型，sql.py 使用。讲解见 docs/12-going-public.md。

两张表，不引入 Alembic——启动时 create_all 建表即可（只建不存在的表，不会给已有的表加列）。
字段与 store/base.py 的 StoredSession / StoredMessage 一一对应。
只用 SQLite 和 Postgres 都支持的类型（JSON、带时区的 DateTime），同一份模型两边通用。
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 访客 id。侧栏按它查「我的会话」，所以建索引
    owner: Mapped[str] = mapped_column(String(64), index=True, default="")
    # 首条用户消息截断即可，不额外调 LLM 生成
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    # 自增整数主键，同时用来排序：同一轮的几条消息 created_at 可能相同（微秒级），
    # 按时间排序会出现不确定的顺序；而 tool 消息必须紧跟在对应的 assistant 消息后面
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # assistant 消息：OpenAI 格式的工具调用
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # tool 消息：对应的调用 id、工具名、是否失败、结构化结果（前端据此复现地图打点）
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_error: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    data: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[Session] = relationship(back_populates="messages")
