"""一次评测运行留下的记录。讲解见 docs/16-evaluation.md。

runner 从 AgentEvent 流里收集这些，checks 只读它们——检查逻辑不碰 loop，
所以可以对着存下来的结果文件重新打分，不用再花 token 重跑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    is_error: bool = False
    data: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """真正拿到了可用结果：协议层没出错，业务层也没返回 {"error": ...}。"""
        return not self.is_error and not (isinstance(self.data, dict) and "error" in self.data)


@dataclass
class TurnRecord:
    user: str
    answer: str = ""
    calls: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def plans(self) -> list[dict[str, Any]]:
        """本轮成功的 plan_pilgrimage 结果。"""
        return [c.data for c in self.calls if c.name == "plan_pilgrimage" and c.ok]


@dataclass
class Trial:
    case_id: str
    trial: int
    turns: list[TurnRecord] = field(default_factory=list)
    latency_s: float = 0.0
    trace_session: str | None = None
    # LLM 接口本身出错（超时、断连）而整条重跑的次数。这是基础设施问题，不该算成 agent 的失败
    infra_retries: int = 0

    @property
    def last(self) -> TurnRecord:
        return self.turns[-1]

    @property
    def error(self) -> str | None:
        return next((t.error for t in self.turns if t.error), None)

    @property
    def total_tokens(self) -> int:
        return sum(t.usage.get("total_tokens", 0) for t in self.turns)

    @property
    def tool_calls(self) -> list[ToolCallRecord]:
        return [c for t in self.turns for c in t.calls]

    @property
    def iterations(self) -> int:
        return sum(t.iterations for t in self.turns)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trial:
        turns = [
            TurnRecord(**{**t, "calls": [ToolCallRecord(**c) for c in t["calls"]]}) for t in d["turns"]
        ]
        return cls(**{**d, "turns": turns})
