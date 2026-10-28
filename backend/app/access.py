"""访问码与匿名访客。讲解见 docs/12-going-public.md。

两件事，都靠 cookie，都不需要账号系统：

1. 访问码（门槛）：知道访问码才能用 /api/chat、/api/sessions。输对一次，服务端发一个
   访问 cookie，之后浏览器自动带上。没配置访问码（本地开发）时不设门槛
2. 访客 id（隔离）：每个浏览器第一次访问 /api 时发一个随机 id，会话记在这个 id 名下，
   侧栏只列自己的会话。清掉 cookie = 换了个人，原来的会话就看不到了

访问 cookie 的值是 HMAC(访问码, 固定串)：
- 无状态：服务端不用记「谁输对过」，重启、多实例都不影响
- 伪造不了：不知道访问码就算不出来
- 换访问码后旧 cookie 自动失效：key 变了，算出来的值就变了
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ACCESS_COOKIE = "aoki_access"
VISITOR_COOKIE = "aoki_visitor"
COOKIE_MAX_AGE = 365 * 24 * 3600
# secrets.token_urlsafe(24) 生成 32 个字符；格式不对的（被篡改、旧版本）当作没有，重新发
VISITOR_ID = re.compile(r"[A-Za-z0-9_-]{32}")


@dataclass(frozen=True)
class AccessConfig:
    code: str = ""  # 空 = 不设门槛
    cookie_secure: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.code)

    def token(self) -> str:
        return hmac.new(self.code.encode(), b"aoki-access-v1", hashlib.sha256).hexdigest()

    def check_code(self, code: str) -> bool:
        # compare_digest：比较耗时与「前几个字符对了几个」无关，不能靠测响应时间逐位猜
        return hmac.compare_digest(code.encode(), self.code.encode())

    def set_cookie(self, response: Response, name: str, value: str) -> None:
        # httponly：页面里的 JS 读不到（就算有 XSS 也偷不走）；samesite=lax：别的网站发起的
        # POST 不带这个 cookie，挡住跨站请求伪造（CSRF）
        response.set_cookie(
            name, value, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=self.cookie_secure
        )


def access_config(request: Request) -> AccessConfig:
    return request.app.state.access


def require_access(request: Request) -> None:
    """挂在受保护的路由上（main.py 的 include_router dependencies）。
    放在依赖里而不是路由函数里：/api/chat 是流式响应，开始之后就没法再返回 401 了。"""
    cfg = access_config(request)
    if not cfg.enabled:
        return
    if not hmac.compare_digest(request.cookies.get(ACCESS_COOKIE, ""), cfg.token()):
        raise HTTPException(status_code=401, detail="需要访问码")


def current_visitor(request: Request) -> str:
    return request.state.visitor_id


class VisitorMiddleware:
    """给访问 /api 的请求确定访客 id，放进 request.state.visitor_id；新访客顺便发 cookie。

    写成纯 ASGI 中间件，而不是 @app.middleware("http")：后者（BaseHTTPMiddleware）会把
    响应体再转一手，对 SSE 这种长连接流式响应容易出问题。这里只在响应头发出时插一个 Set-Cookie，
    响应体原样透传。"""

    def __init__(self, app: ASGIApp, cookie_secure: bool = False) -> None:
        self.app = app
        self.cookie_secure = cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return

        visitor = HTTPConnection(scope).cookies.get(VISITOR_COOKIE, "")
        is_new = not VISITOR_ID.fullmatch(visitor)
        if is_new:
            visitor = secrets.token_urlsafe(24)
        scope.setdefault("state", {})["visitor_id"] = visitor
        if not is_new:
            await self.app(scope, receive, send)
            return

        cookie = Response()
        AccessConfig(cookie_secure=self.cookie_secure).set_cookie(cookie, VISITOR_COOKIE, visitor)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", cookie.headers["set-cookie"])
            await send(message)

        await self.app(scope, receive, send_with_cookie)
