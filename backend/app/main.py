"""FastAPI 入口。讲解见 docs/08-fastapi-sse.md。

    uvicorn backend.app.main:app --reload

create_app() 接受一个「组件工厂」：生产环境用 default_components（真 LLM、真 MCP 子进程），
测试传入一个返回假 LLM / 假工具的工厂。两者走的是完全相同的路由代码。
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from .access import AccessConfig, VisitorMiddleware, require_access
from .chat_service import ChatService
from .components import Components, default_components
from .config import settings
from .routes import access, chat, sessions

ComponentsFactory = Callable[[], AbstractAsyncContextManager[Components]]

# npm run build 的产物。讲解见 docs/10-frontend.md
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    components_factory: ComponentsFactory | None = None, access_cfg: AccessConfig | None = None
) -> FastAPI:
    factory = components_factory or (lambda: default_components(settings))
    access_cfg = access_cfg or AccessConfig(settings.access_code, settings.cookie_secure)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # yield 之前：启动（拉起 MCP 子进程等）；yield 之后：关闭。
        # 两者在同一个函数、同一个 task 里——MCP SDK 要求如此（见 docs/07）
        async with factory() as components:
            app.state.service = ChatService(components)
            yield

    app = FastAPI(title="Seichi-junrei Agent", lifespan=lifespan)
    app.state.access = access_cfg
    # 访客 id 与访问码，讲解见 docs/12-going-public.md
    app.add_middleware(VisitorMiddleware, cookie_secure=access_cfg.cookie_secure)
    # 业务接口统一挂在 /api 下，根路径 / 留给前端页面。
    # 开发时 Vite 只需代理 /api 一条规则；部署时一个容器同时出页面和接口，同源、无需 CORS。
    # 路由级依赖先于路由自己的依赖执行：没有访问码的请求在 resolve_chat_session 新建会话之前就被挡下
    gated = [Depends(require_access)]
    app.include_router(chat.router, prefix="/api", dependencies=gated)
    app.include_router(sessions.router, prefix="/api", dependencies=gated)
    app.include_router(access.router, prefix="/api")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # 必须最后挂：mount("/") 会接住所有没被上面路由匹配的路径。
    # 没有构建产物（只跑后端、跑测试）时跳过，不影响接口
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()
