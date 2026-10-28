"""读取并校验评测用例（evals/cases.yaml）。讲解见 docs/16-evaluation.md。

校验很严格：expect 里写错一个键（比如 must_cal）会直接报错，而不是被静默忽略——
否则那条检查永远不执行，用例永远「通过」，这种错误最难发现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from backend.agent.prompts import JST

CASES_FILE = Path(__file__).parent / "cases.yaml"

# 评测时的「当前时间」固定下来：system prompt 里有今天的日期，用户说「周六」「明天」时
# LLM 据此换算。不固定的话，同一条用例每天传的 date 参数都不一样，结果没法比较
EVAL_NOW = datetime(2026, 10, 28, 9, 0, tzinfo=JST)  # 周三

ALLOWED = {
    "tools": {"must_call", "must_not_call", "order", "max_calls", "max_per_tool"},
    "args": None,  # 键是工具名，任意
    "plan": {"ok", "start_near", "min_stops", "area_changed"},
    "answer": {"contains_any", "contains_all", "not_contains", "asks_question", "language", "mentions_stops"},
}
MATCHER_KEYS = {"one_of", "range", "present"}


@dataclass
class Case:
    id: str
    category: str
    turns: list[str]
    expect: dict[str, Any] = field(default_factory=dict)
    faults: dict[str, str] = field(default_factory=dict)


def load_cases(path: Path = CASES_FILE, now: datetime = EVAL_NOW) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [_parse(item, now) for item in raw["cases"]]
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"用例 id 重复：{sorted(dupes)}")
    return cases


def _parse(item: dict[str, Any], now: datetime) -> Case:
    case = Case(
        id=item["id"],
        category=item["category"],
        turns=list(item["turns"]),
        expect=item.get("expect") or {},
        faults=item.get("faults") or {},
    )
    unknown = set(item) - {"id", "category", "turns", "expect", "faults"}
    if unknown:
        raise ValueError(f"{case.id}: 未知字段 {sorted(unknown)}")
    if not case.turns:
        raise ValueError(f"{case.id}: turns 不能为空")
    for section, body in case.expect.items():
        if section not in ALLOWED:
            raise ValueError(f"{case.id}: expect 里有未知部分 {section}")
        if ALLOWED[section] is not None and set(body) - ALLOWED[section]:
            raise ValueError(f"{case.id}: expect.{section} 里有未知检查 {sorted(set(body) - ALLOWED[section])}")
    for tool, params in case.expect.get("args", {}).items():
        case.expect["args"][tool] = {k: _resolve(v, now, case.id) for k, v in params.items()}
    return case


def _resolve(value: Any, now: datetime, case_id: str) -> Any:
    """把 "@date:+1" / "@date:sat" 换成具体日期；顺便校验 matcher 的写法。"""
    if isinstance(value, dict):
        if len(value) != 1 or set(value) - MATCHER_KEYS:
            raise ValueError(f"{case_id}: matcher 只能是 {sorted(MATCHER_KEYS)} 之一，实际 {value}")
        return value
    if isinstance(value, str) and value.startswith("@date:"):
        return resolve_date(value[len("@date:"):], now)
    return value


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def resolve_date(spec: str, now: datetime) -> str:
    if spec.startswith("+"):
        day = now + timedelta(days=int(spec[1:]))
    elif spec in WEEKDAYS:
        # 「周六」= 今天之后最近的那个周六（今天就是周六时指下周六——和多数人的说法一致）
        ahead = (WEEKDAYS.index(spec) - now.weekday() - 1) % 7 + 1
        day = now + timedelta(days=ahead)
    else:
        raise ValueError(f"不认识的日期写法：@date:{spec}")
    return day.strftime("%Y%m%d")
