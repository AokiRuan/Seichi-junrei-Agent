"""SessionStore 的 SQL 实现：SQLite（本地持久化）和 Postgres（线上）共用。讲解见 docs/12-going-public.md。

    DATABASE_URL=sqlite+aiosqlite:///aoki.db                       # 本地，一个文件
    DATABASE_URL=postgresql+asyncpg://aoki:aoki@localhost:5432/aoki  # 线上 / docker

为什么一份代码两种库：SQLAlchemy 屏蔽了方言差异，models.py 只用两边都有的类型。
测试用 SQLite 跑（不需要起数据库服务），线上用 Postgres——容器的文件系统是临时的，
重新部署就丢，所以线上数据库必须在容器外，只通过连接串访问。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .base import SessionNotFound, SessionStore, StoredMessage, StoredSession, utcnow
from .models import Base, Message, Session


def normalize_database_url(url: str) -> str:
    """托管 Postgres（Neon、Supabase、RDS）给的连接串是 postgres:// 或 postgresql://，
    SQLAlchemy 默认会用同步驱动 psycopg2 去连。这里改成 asyncpg，并把 libpq 风格的
    sslmode 参数换成 asyncpg 认识的 ssl（否则连接时报「unexpected keyword argument 'sslmode'」）。"""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("sslmode=", "ssl=")
    return url


class SqlSessionStore(SessionStore):
    def __init__(self, database_url: str) -> None:
        # pool_pre_ping：拿连接前先探一下。托管 Postgres 会掐掉空闲连接，
        # 不探的话，服务闲置一阵后的第一个请求会拿到一条已断开的连接而报错
        self._engine: AsyncEngine = create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)
        # expire_on_commit=False：提交后对象的属性仍可读。默认会在提交后过期，
        # 再读属性要回数据库查——而那时会话已经关了，async 下直接报错
        self._db = async_sessionmaker(self._engine, expire_on_commit=False)

    async def open(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_session(self, title: str, owner: str) -> StoredSession:
        now = utcnow()
        row = Session(id=str(uuid.uuid4()), owner=owner, title=title, created_at=now, updated_at=now)
        async with self._db.begin() as db:
            db.add(row)
        return _to_session(row)

    async def get_session(self, session_id: str) -> StoredSession | None:
        async with self._db() as db:
            row = await db.get(Session, session_id)
        return _to_session(row) if row else None

    async def list_sessions(self, owner: str) -> list[StoredSession]:
        stmt = select(Session).where(Session.owner == owner).order_by(Session.updated_at.desc())
        async with self._db() as db:
            rows = (await db.scalars(stmt)).all()
        return [_to_session(r) for r in rows]

    async def append_messages(self, session_id: str, messages: list[StoredMessage]) -> None:
        # begin()：整个块是一个事务，正常退出时提交，抛异常时回滚。
        # 一轮的消息要么全存上，要么都不存（见 base.py 的说明）
        async with self._db.begin() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.updated_at = utcnow()
            db.add_all(Message(session_id=session_id, **m.model_dump()) for m in messages)

    async def get_messages(self, session_id: str) -> list[StoredMessage]:
        # 按自增 id 排序，不按时间：同一轮的几条消息时间可能相同（见 models.py）
        stmt = select(Message).where(Message.session_id == session_id).order_by(Message.id)
        async with self._db() as db:
            if await db.get(Session, session_id) is None:
                raise SessionNotFound(session_id)
            rows = (await db.scalars(stmt)).all()
        return [_to_message(r) for r in rows]


def _utc(dt: datetime) -> datetime:
    """SQLite 没有带时区的时间类型，存进去的 UTC 时间读出来是 naive 的（没有 tzinfo）。
    补上 UTC，让两种库返回的时间一致——否则前端会把它当成本地时间，侧栏显示的「几分钟前」差出时区。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_session(row: Session) -> StoredSession:
    return StoredSession(
        id=row.id, title=row.title, owner=row.owner,
        created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
    )


def _to_message(row: Message) -> StoredMessage:
    return StoredMessage(
        role=row.role, content=row.content, tool_calls=row.tool_calls, tool_call_id=row.tool_call_id,
        tool_name=row.tool_name, is_error=row.is_error, data=row.data, created_at=_utc(row.created_at),
    )
