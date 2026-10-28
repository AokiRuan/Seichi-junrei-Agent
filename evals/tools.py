"""评测用的两层工具包装：响应缓存、故障注入。讲解见 docs/16-evaluation.md。

和 tracing.py 一样是装饰器：都实现 ToolExecutor（tool_schemas + call_tool），loop 看不出区别。

    FaultyTools(CachedTools(MCPClientPool))

- CachedTools：同样的工具 + 同样的参数，直接返回上次的结果。天气、geocode、电车换乘都是
  外部服务，今天和明天的返回不一样、偶尔还超时；不缓存的话，分数的变化分不清是 agent 变了
  还是外部世界变了。缓存还顺带省掉了对公共服务的重复请求
- FaultyTools：让指定工具一律失败，测 agent 在工具出错时能不能体面地收场
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from backend.agent.harness import Harness
from backend.agent.mcp_client import PROJECT_ROOT, ToolExecutor, ToolOutcome
from backend.mcp_servers.seichi.anitabi import SNAPSHOT_FILE

CACHE_FILE = Path(__file__).parent / ".cache" / "tool_cache.json"


def tools_fingerprint() -> str:
    """工具实现的指纹：所有 MCP server 的源码 + 快照文件的大小和修改时间。

    改了规划算法却还拿旧缓存，评测测的就是旧代码——所以缓存条目带上指纹，
    指纹变了整条作废。粒度粗（改天气 server 也会让圣地的缓存失效），但不会出错
    """
    h = hashlib.sha256()
    for path in sorted((PROJECT_ROOT / "backend" / "mcp_servers").rglob("*.py")):
        h.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        h.update(path.read_bytes())
    if SNAPSHOT_FILE.exists():
        st = SNAPSHOT_FILE.stat()
        h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


class CachedTools:
    def __init__(self, inner: ToolExecutor, path: Path = CACHE_FILE, fingerprint: str | None = None) -> None:
        self._inner = inner
        self._path = path
        self.fingerprint = fingerprint or tools_fingerprint()
        self._entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            self._entries = json.loads(path.read_text(encoding="utf-8"))
        self.hits = 0
        self.misses = 0

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._inner.tool_schemas()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        key = Harness.call_key(name, arguments)
        entry = self._entries.get(key)
        if entry and entry["fp"] == self.fingerprint:
            self.hits += 1
            return ToolOutcome(data=entry["data"], is_error=False, text=entry["text"])
        self.misses += 1
        outcome = await self._inner.call_tool(name, arguments)
        # 只缓存成功的结果：超时这类偶发失败不该被固定下来，下次运行还有机会成功
        if not outcome.is_error:
            self._entries[key] = {"fp": self.fingerprint, "data": outcome.data, "text": outcome.text}
        return outcome

    def save(self) -> None:
        # 指纹不同的旧条目顺手清掉，文件不会越积越大
        live = {k: v for k, v in self._entries.items() if v["fp"] == self.fingerprint}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")


class FaultyTools:
    def __init__(self, inner: ToolExecutor, faults: dict[str, str]) -> None:
        self._inner = inner
        self._faults = faults

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._inner.tool_schemas()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name in self._faults:
            # 和 MCPClientPool 真实出错时的形状一致：is_error=True、data 为空、text 是给 LLM 的说明
            return ToolOutcome(data=None, is_error=True, text=self._faults[name])
        return await self._inner.call_tool(name, arguments)
