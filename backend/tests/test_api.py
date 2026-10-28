"""HTTP 接口测试：注入假 LLM / 假工具，走真实的路由、SSE 编码与会话存储。
讲解见 docs/08-fastapi-sse.md。
"""
import json
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from backend.agent.harness import Harness
from backend.agent.loop import AgentLoop
from backend.agent.tracing import Tracing
from backend.app.access import ACCESS_COOKIE, VISITOR_COOKIE, AccessConfig
from backend.app.components import Components
from backend.app import main
from backend.app.main import create_app
from backend.store.memory import InMemorySessionStore
from backend.tests.fakes import FakeLLM, FakeTools, answer, call, with_tools


def make_client(llm, tools=None, access_code=""):
    store = InMemorySessionStore()

    @asynccontextmanager
    async def factory():
        loop = AgentLoop(llm, tools or FakeTools(), Harness())
        yield Components(loop=loop, store=store, tracing=Tracing(None))

    # 显式传 AccessConfig：不受本机 .env 里 ACCESS_CODE 的影响
    return TestClient(create_app(factory, AccessConfig(code=access_code)))


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """把 text/event-stream 的原始文本解析成 [(事件名, 数据)]。以 ':' 开头的是保活注释，跳过。"""
    events = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if data is not None:
            events.append((name, data))
    return events


def chat(client, message, session_id=None):
    body = {"message": message} | ({"session_id": session_id} if session_id else {})
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        raw = r.read().decode("utf-8")
        assert "\\u" not in raw  # 中文原样传输，没有被转义成 \uXXXX
        return parse_sse(raw)


def names(events):
    out = []
    for n, _ in events:
        if not (n == "token" and out and out[-1] == "token"):
            out.append(n)
    return out


def test_new_session_full_event_stream():
    llm = FakeLLM(with_tools(call("c1", title="莉可丽丝"), content="我来查一下"), answer("找到了 5 处圣地"))
    with make_client(llm) as client:
        events = chat(client, "莉可丽丝的圣地在哪？")

    assert names(events) == ["session", "token", "tool_call", "tool_result", "token", "done"]
    assert "\\u" not in str(events)  # 中文原样传输，没有被转义
    session = events[0][1]
    assert session["title"] == "莉可丽丝的圣地在哪？" and session["session_id"]

    text = "".join(d["content"] for n, d in events if n == "token")
    assert text == "我来查一下找到了 5 处圣地"

    call_ev = next(d for n, d in events if n == "tool_call")
    assert call_ev == {"tool_name": "search_anime", "tool_call_id": "c1", "payload": {"title": "莉可丽丝"}}
    result_ev = next(d for n, d in events if n == "tool_result")
    assert result_ev["payload"] == {"is_error": False, "data": {"ok": "search_anime"}}

    done = events[-1][1]["payload"]
    assert "new_messages" not in done  # 内部入库用的，不发给前端
    assert done["iterations"] == 2


def test_second_turn_sends_history_and_session_restores_tool_data():
    llm = FakeLLM(
        with_tools(call("c1", title="莉可丽丝")), answer("第一轮回答"),
        answer("第二轮回答"),
    )
    with make_client(llm) as client:
        sid = chat(client, "第一轮")[0][1]["session_id"]
        second = chat(client, "第二轮", session_id=sid)

        assert second[0][1]["session_id"] == sid  # 沿用同一个会话
        sent = llm.requests[-1]["messages"]
        assert sent[0]["role"] == "system"
        assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool", "assistant", "user"]
        # 发给 LLM 的历史里不能有只给前端用的字段
        assert "data" not in sent[3] and "tool_name" not in sent[3]

        listed = client.get("/api/sessions").json()
        assert [s["id"] for s in listed] == [sid]

        detail = client.get(f"/api/sessions/{sid}").json()
        tool_msg = next(m for m in detail["messages"] if m["role"] == "tool")
        # 恢复会话时前端拿得到工具名和结构化结果，能重新在地图上打点
        assert tool_msg["tool_name"] == "search_anime"
        assert tool_msg["data"] == {"ok": "search_anime"}


def test_unknown_session_is_404_before_streaming():
    with make_client(FakeLLM()) as client:
        r = client.post("/api/chat", json={"message": "hi", "session_id": "nope"})
        assert r.status_code == 404
        assert client.get("/api/sessions/nope").status_code == 404


def test_empty_message_rejected():
    with make_client(FakeLLM()) as client:
        assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_llm_error_event_and_turn_not_persisted():
    with make_client(FakeLLM(RuntimeError("401 Unauthorized"))) as client:
        events = chat(client, "hi")
        assert names(events) == ["session", "error"]
        assert "401" in events[-1][1]["message"]
        sid = events[0][1]["session_id"]
        assert client.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_frontend_served_at_root_when_built(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>前端</title>", encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path)
    with make_client(FakeLLM()) as client:
        assert "<title>前端</title>" in client.get("/").text
        # 接口在 /api 下，不会被静态文件挂载吃掉
        assert client.get("/api/sessions").json() == []
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/no-such-file.js").status_code == 404


def test_no_frontend_build_api_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "missing")
    with make_client(FakeLLM()) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/sessions").status_code == 200


# ---------------------------------------------------------------- 访问码与访客隔离（docs/12-going-public.md）


def test_sessions_are_isolated_per_visitor():
    llm = FakeLLM(answer("A 的回答"), answer("B 的回答"))
    with make_client(llm) as client:
        sid_a = chat(client, "我是 A")[0][1]["session_id"]
        visitor_a = client.cookies[VISITOR_COOKIE]
        assert [s["id"] for s in client.get("/api/sessions").json()] == [sid_a]
        # owner 只在服务端用，不出现在响应里
        assert "owner" not in client.get("/api/sessions").json()[0]
        assert "owner" not in client.get(f"/api/sessions/{sid_a}").json()

        client.cookies.clear()  # 换一个浏览器
        assert client.get("/api/sessions").json() == []
        assert client.cookies[VISITOR_COOKIE] != visitor_a  # 新访客拿到新 id
        # 知道别人的会话 id 也读不到、续不上——和不存在一样是 404
        assert client.get(f"/api/sessions/{sid_a}").status_code == 404
        assert client.post("/api/chat", json={"message": "偷看", "session_id": sid_a}).status_code == 404
        chat(client, "我是 B")
        assert [s["title"] for s in client.get("/api/sessions").json()] == ["我是 B"]


def test_malformed_visitor_cookie_is_replaced():
    with make_client(FakeLLM()) as client:
        r = client.get("/api/sessions", headers={"cookie": f"{VISITOR_COOKIE}=short"})
        assert r.status_code == 200
        issued = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        assert len(issued) == 32


def test_visitor_cookie_only_issued_under_api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "missing")
    with make_client(FakeLLM()) as client:
        r = client.get("/health")
        assert "set-cookie" not in r.headers
        r = client.get("/api/sessions")
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_access_code_gates_api():
    llm = FakeLLM(answer("你好"))
    with make_client(llm, access_code="sakura-2026") as client:
        assert client.get("/api/sessions").status_code == 401
        r = client.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 401 and r.json()["detail"] == "需要访问码"
        assert client.get("/health").status_code == 200  # 健康检查不设门槛，部署平台要探活

        assert client.post("/api/access", json={"code": "wrong"}).status_code == 401
        assert ACCESS_COOKIE not in client.cookies
        # 伪造的 cookie 不行
        assert client.get("/api/sessions", headers={"cookie": f"{ACCESS_COOKIE}=forged"}).status_code == 401

        r = client.post("/api/access", json={"code": "sakura-2026"})
        assert r.status_code == 204
        assert "sakura" not in client.cookies[ACCESS_COOKIE]  # cookie 里是 HMAC，不是访问码本身
        assert client.get("/api/sessions").json() == []
        assert names(chat(client, "hi"))[-1] == "done"


def test_rejected_chat_creates_no_session():
    """访问码检查（路由级依赖）必须先于 resolve_chat_session（会新建会话）执行。"""
    store = InMemorySessionStore()

    @asynccontextmanager
    async def factory():
        yield Components(loop=AgentLoop(FakeLLM(), FakeTools(), Harness()), store=store, tracing=Tracing(None))

    with TestClient(create_app(factory, AccessConfig(code="x"))) as client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert store._sessions == {}


def test_changing_access_code_invalidates_old_cookie():
    with make_client(FakeLLM(), access_code="old") as client:
        client.post("/api/access", json={"code": "old"})
        old_cookie = client.cookies[ACCESS_COOKIE]
    with make_client(FakeLLM(), access_code="new") as client:
        assert client.get("/api/sessions", headers={"cookie": f"{ACCESS_COOKIE}={old_cookie}"}).status_code == 401


def test_access_endpoint_is_noop_when_disabled():
    with make_client(FakeLLM()) as client:
        assert client.post("/api/access", json={"code": "anything"}).status_code == 204
        assert ACCESS_COOKIE not in client.cookies
