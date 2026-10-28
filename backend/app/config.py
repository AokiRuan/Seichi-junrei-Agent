"""配置。全部从环境变量读取，本地用 .env，生产走 App Runner 环境变量。

讲解见 docs/02-project-setup.md。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"

    # 数据库。为空时回退到内存 store，方便本地不起数据库也能开发。
    # sqlite+aiosqlite:///aoki.db（本地文件）或 postgresql://...（线上），见 docs/12-going-public.md
    database_url: str = ""

    # 访问码。为空 = 不设门槛（本地开发）；公网部署时必须设置，否则谁都能消耗你的 LLM 额度
    access_code: str = ""
    # cookie 只走 HTTPS。线上（HTTPS）设为 true；本地是 http，设了浏览器就不会回传 cookie
    cookie_secure: bool = False

    # Langfuse（未配置时静默跳过）
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Agent
    max_iterations: int = 8


settings = Settings()
