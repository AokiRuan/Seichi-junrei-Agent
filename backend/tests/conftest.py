"""pytest 全局配置：在任何测试模块 import 之前执行。

.env 里配了真实的 Langfuse key。测试（尤其是会走 lifespan 的 API 测试）不应该往线上项目
写数据，所以这里用空的环境变量覆盖掉——pydantic-settings 里环境变量优先级高于 .env。

圣地数据固定用 mock：本地有没有 Anitabi 快照、快照是哪天下载的，都不应该影响测试结果。
必须在这里设、而不是在 fixture 里：seichi/server.py 在 import 时就选好了数据源。
子进程（MCP client 测试）通过 PASSTHROUGH_ENV 拿到同一个值。快照本身的测试见 test_anitabi.py
"""
import os

os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["SEICHI_DATA_SOURCE"] = "mock"
