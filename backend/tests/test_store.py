"""会话存储测试。讲解见 docs/08-fastapi-sse.md、docs/12-going-public.md。

同一组用例跑三种实现：内存、SQLite（临时文件）、Postgres（设置了 TEST_DATABASE_URL 才跑）。
三者行为一致，ChatService 才能对存储一无所知。

    $env:TEST_DATABASE_URL = "postgresql+asyncpg://aoki:aoki@localhost:5432/aoki"; python -m pytest backend/tests/test_store.py
"""
import os
from datetime import timezone

import pytest

from backend.store.base import SessionNotFound, StoredMessage
from backend.store.memory import InMemorySessionStore
from backend.store.models import Base
from backend.store.sql import SqlSessionStore, normalize_database_url

PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=[
    "memory",
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="未设置 TEST_DATABASE_URL")),
])
async def store(request, tmp_path):
    if request.param == "memory":
        yield InMemorySessionStore()
        return
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}" if request.param == "sqlite" else PG_URL
    s = SqlSessionStore(url)
    if request.param == "postgres":  # 共用一个库：每条用例从空表开始
        async with s._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await s.open()
    yield s
    await s.close()


async def test_create_append_and_read_back(store):
    s = await store.create_session("莉可丽丝的圣地", owner="v1")
    await store.append_messages(s.id, [
        StoredMessage(role="user", content="莉可丽丝的圣地"),
        StoredMessage(role="assistant", content=None, tool_calls=[{"id": "c1"}]),
        StoredMessage(role="tool", content="[...]", tool_call_id="c1", tool_name="search_anime",
                      is_error=False, data=[{"anime_id": 364450}]),
    ])
    msgs = await store.get_messages(s.id)
    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1].tool_calls == [{"id": "c1"}]
    assert msgs[2].data == [{"anime_id": 364450}] and msgs[2].is_error is False
    got = await store.get_session(s.id)
    assert got.title == "莉可丽丝的圣地" and got.owner == "v1"


async def test_list_only_own_sessions_sorted_by_recent_update(store):
    a = await store.create_session("a", owner="v1")
    b = await store.create_session("b", owner="v1")
    await store.create_session("别人的", owner="v2")
    await store.append_messages(a.id, [StoredMessage(role="user", content="hi")])
    assert [s.id for s in await store.list_sessions("v1")] == [a.id, b.id]
    assert [s.title for s in await store.list_sessions("v2")] == ["别人的"]
    assert await store.list_sessions("nobody") == []


async def test_unknown_session(store):
    assert await store.get_session("nope") is None
    with pytest.raises(SessionNotFound):
        await store.append_messages("nope", [])
    with pytest.raises(SessionNotFound):
        await store.get_messages("nope")


async def test_times_are_utc_aware(store):
    # SQLite 读回来的时间没有时区，store 要补上；否则前端会把 UTC 当本地时间显示
    s = await store.create_session("x", owner="v1")
    await store.append_messages(s.id, [StoredMessage(role="user", content="hi")])
    got = (await store.list_sessions("v1"))[0]
    assert got.updated_at.tzinfo is not None
    assert got.updated_at.utcoffset() == timezone.utc.utcoffset(None)
    assert (await store.get_messages(s.id))[0].created_at.tzinfo is not None


async def test_returned_list_is_a_copy(store):
    s = await store.create_session("x", owner="v1")
    (await store.get_messages(s.id)).append(StoredMessage(role="user", content="leak"))
    assert await store.get_messages(s.id) == []


async def test_sqlite_survives_reopen(tmp_path):
    """持久化的意义：进程重启（这里是重新建 store）后数据还在。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
    first = SqlSessionStore(url)
    await first.open()
    s = await first.create_session("重启前", owner="v1")
    await first.append_messages(s.id, [StoredMessage(role="user", content="hi")])
    await first.close()

    second = SqlSessionStore(url)
    await second.open()  # create_all 不会动已有的表和数据
    assert [x.title for x in await second.list_sessions("v1")] == ["重启前"]
    assert [m.content for m in await second.get_messages(s.id)] == ["hi"]
    await second.close()


def test_normalize_database_url():
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db?sslmode=require") == "postgresql+asyncpg://u:p@h/db?ssl=require"
    assert normalize_database_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_database_url("sqlite+aiosqlite:///aoki.db") == "sqlite+aiosqlite:///aoki.db"


def test_to_llm_message_drops_ui_only_fields():
    m = StoredMessage(role="tool", content="txt", tool_call_id="c1", tool_name="x", is_error=False, data={"a": 1})
    # LLM 只认识 role / content / tool_call_id；其余字段发过去轻则浪费 token，重则被拒绝
    assert m.to_llm_message() == {"role": "tool", "content": "txt", "tool_call_id": "c1"}
