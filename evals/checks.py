"""给一次运行打分：全部是确定性的规则检查。讲解见 docs/16-evaluation.md。

两类检查（借鉴 TravelPlanner 的「常识约束 / 硬约束」）：
- 通用检查：每条用例都做。能跑完、不写经纬度、用对语言；只要规划成功了，行程本身要自洽
  （不超预算、不重复、时间对得上）——这是 agent 无论被问什么都不该违反的
- 用例检查：cases.yaml 里每条用例的 expect，表达「这个问题该怎么答」

判定尽量看工具调用和工具返回的结构化数据（借鉴 τ-bench 看数据库终态而不是看回复文字），
只有确实只能从文字里看的（语言、有没有追问、有没有编天气）才检查回答文本。

检查名的前缀就是失败分类：delivery / tools / args / plan / answer。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from backend.mcp_servers.seichi.planner import MAX_WALK_LEG_MIN

from .cases import Case
from .records import ToolCallRecord, Trial

COORD_RE = re.compile(r"\d{2,3}\.\d{4,}")
# 追问不一定带问号。实测：「还需要知道你想巡礼哪部作品……告诉我就好。」
QUESTION_CUES = ("？", "?", "告诉我", "请问", "哪部", "需要知道")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    @property
    def category(self) -> str:
        return self.name.split(".")[0]


def run_checks(case: Case, trial: Trial) -> list[CheckResult]:
    out = [_delivered(trial)]
    if out[0].passed:
        out += _generic(case, trial)
        out += _tool_checks(case.expect.get("tools", {}), trial.last.calls)
        out += _arg_checks(case.expect.get("args", {}), trial.last.calls)
        out += _plan_checks(case.expect.get("plan", {}), trial)
        out += _answer_checks(case.expect.get("answer", {}), trial)
    return out


# ------------------------------------------------------------------ 通用

def _delivered(trial: Trial) -> CheckResult:
    """TravelPlanner 的 Delivery Rate：在步数上限内交出了回答（没报错、不是空回复）。"""
    if trial.error:
        return CheckResult("delivery", False, trial.error)
    empty = [i for i, t in enumerate(trial.turns, 1) if not t.answer.strip()]
    return CheckResult("delivery", not empty, f"第 {empty} 轮回答为空" if empty else "")


def _generic(case: Case, trial: Trial) -> list[CheckResult]:
    answer = trial.last.answer
    coord = COORD_RE.search(answer)
    out = [
        _check("answer.no_coordinates", coord is None, coord.group() if coord else ""),
        _language(case.expect.get("answer", {}).get("language", "zh"), answer),
    ]
    for plan in trial.last.plans():
        out += plan_validity(plan)
    return out


def plan_validity(plan: dict[str, Any]) -> list[CheckResult]:
    """行程自洽：这些由确定性代码保证，失败说明 planner / itinerary 有 bug，不是 LLM 的问题。"""
    stops = plan.get("stops") or []
    ids = [s["spot_id"] for s in stops]
    access = (plan.get("access") or {}).get("duration_min", 0)
    walk = sum(s.get("walk_from_prev_min", 0) for s in stops)
    dwell = sum(s.get("dwell_min", 0) for s in stops)
    clocks = [s["arrive_at"] for s in stops if "arrive_at" in s]
    long_legs = [s.get("walk_from_prev_min", 0) for s in stops if s.get("walk_from_prev_min", 0) > MAX_WALK_LEG_MIN]
    # 只够去第一站时（坐车就用掉了大半预算），超预算是明确告知过的，不算错
    over_ok = len(stops) == 1 and any("只安排了到达的第一处" in a for a in plan.get("assumptions", []))
    return [
        _check("plan.budget", plan["total_min"] <= plan["budget_min"] or over_ok,
               f"{plan['total_min']} > {plan['budget_min']} 分钟"),
        _check("plan.unique_stops", len(ids) == len(set(ids)), f"重复：{ids}"),
        _check("plan.order", [s["order"] for s in stops] == list(range(1, len(stops) + 1)), "序号不连续"),
        _check("plan.clock", clocks == sorted(clocks), f"到达时刻倒退：{clocks}"),
        _check("plan.walk_legs", not long_legs, f"单段步行超过 {MAX_WALK_LEG_MIN} 分钟：{long_legs}"),
        # 每段都四舍五入过，允许每站 1 分钟误差
        _check("plan.total_consistent", abs(access + walk + dwell - plan["total_min"]) <= len(stops) + 1,
               f"交通 {access} + 步行 {walk} + 停留 {dwell} ≠ 总计 {plan['total_min']}"),
    ]


# ------------------------------------------------------------------ 工具选择

def _tool_checks(spec: dict[str, Any], calls: list[ToolCallRecord]) -> list[CheckResult]:
    names = [c.name for c in calls]
    out = []
    for tool in spec.get("must_call", []):
        out.append(_check(f"tools.must_call.{tool}", tool in names, f"实际调用：{names}"))
    for tool in spec.get("must_not_call", []):
        out.append(_check(f"tools.must_not_call.{tool}", tool not in names, f"实际调用：{names}"))
    if "order" in spec:
        firsts = [names.index(t) for t in spec["order"] if t in names]
        ok = len(firsts) == len(spec["order"]) and firsts == sorted(firsts)
        out.append(_check("tools.order", ok, f"期望顺序 {spec['order']}，实际 {names}"))
    if "max_calls" in spec:
        out.append(_check("tools.max_calls", len(calls) <= spec["max_calls"],
                          f"{len(calls)} 次 > {spec['max_calls']}"))
    for tool, limit in spec.get("max_per_tool", {}).items():
        n = names.count(tool)
        out.append(_check(f"tools.max_per_tool.{tool}", n <= limit, f"{n} 次 > {limit}"))
    return out


# ------------------------------------------------------------------ 参数

def _arg_checks(spec: dict[str, dict[str, Any]], calls: list[ToolCallRecord]) -> list[CheckResult]:
    out = []
    for tool, expected in spec.items():
        seen = [c.args for c in calls if c.name == tool]
        ok = any(all(match(args, k, m) for k, m in expected.items()) for args in seen)
        out.append(_check(f"args.{tool}", ok, f"期望 {expected}，实际 {seen or '没有调用'}"))
    return out


def match(args: dict[str, Any], key: str, matcher: Any) -> bool:
    present = args.get(key) is not None
    if isinstance(matcher, dict):
        if "present" in matcher:
            return present == matcher["present"]
        if not present:
            return False
        value = args[key]
        if "one_of" in matcher:
            return value in matcher["one_of"]
        lo, hi = matcher["range"]
        return isinstance(value, (int, float)) and lo <= value <= hi
    return present and args[key] == matcher


# ------------------------------------------------------------------ 行程（看工具返回的终态）

def _plan_checks(spec: dict[str, Any], trial: Trial) -> list[CheckResult]:
    if not spec:
        return []
    plans = trial.last.plans()
    plan = plans[-1] if plans else None
    out = []
    if spec.get("ok"):
        errors = [c.error or (c.data or {}).get("error") for c in trial.last.calls if c.name == "plan_pilgrimage"]
        out.append(_check("plan.ok", plan is not None, f"没有成功的规划结果：{errors or '没有调用'}"))
    if plan is None:
        return out
    if "start_near" in spec:
        lat, lng, km = spec["start_near"]
        start = plan.get("start")
        dist = _km(lat, lng, start["lat"], start["lng"]) if start else None
        out.append(_check("plan.start_near", dist is not None and dist <= km,
                          "没有起点" if dist is None else f"起点离期望位置 {dist:.1f} km"))
    if "min_stops" in spec:
        n = len(plan.get("stops", []))
        out.append(_check("plan.min_stops", n >= spec["min_stops"], f"只有 {n} 站"))
    if spec.get("area_changed"):
        before = [p["area"]["id"] for t in trial.turns[:-1] for p in t.plans()]
        now = plan["area"]["id"]
        out.append(_check("plan.area_changed", bool(before) and now not in before,
                          f"之前 {before}，现在 {now}"))
    return out


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    # 等距圆柱近似：几公里的范围内足够准
    dx = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    dy = math.radians(lat2 - lat1)
    return 6371 * math.hypot(dx, dy)


# ------------------------------------------------------------------ 回答文本

def _answer_checks(spec: dict[str, Any], trial: Trial) -> list[CheckResult]:
    answer = trial.last.answer
    out = []
    if "contains_any" in spec:
        out.append(_check("answer.contains_any", any(w in answer for w in spec["contains_any"]),
                          f"一个都没出现：{spec['contains_any']}"))
    for w in spec.get("contains_all", []):
        out.append(_check("answer.contains_all", w in answer, f"缺少「{w}」"))
    hits = [w for w in spec.get("not_contains", []) if _affirmed(w, answer)]
    if "not_contains" in spec:
        out.append(_check("answer.not_contains", not hits, f"出现了 {hits}"))
    if "asks_question" in spec:
        asked = any(cue in answer for cue in QUESTION_CUES)
        out.append(_check("answer.asks_question", asked == spec["asks_question"], "没有问句"))
    if "mentions_stops" in spec:
        plans = trial.last.plans()
        stops = plans[-1]["stops"] if plans else []
        named = [s for s in stops if _mentioned(s, answer)]
        out.append(_check("answer.mentions_stops", len(named) >= spec["mentions_stops"],
                          f"只提到 {len(named)} 个行程里的站点"))
    return out


NEGATIONS = "不没未非无"


def _affirmed(word: str, text: str) -> bool:
    """word 出现了、且不是被否定的说法。实测：回答写「不含返程」，按子串判断会命中「含返程」。

    往前看两个字，「不包含返程」也算否定
    """
    return any(
        not any(ch in NEGATIONS for ch in text[max(0, m.start() - 2) : m.start()])
        for m in re.finditer(re.escape(word), text)
    )


def _mentioned(stop: dict[str, Any], answer: str) -> bool:
    """回答里提到了这一站：中文名或日文名出现，或者名字的前 3 个字出现（「下北沢駅南口」→「下北沢」）。"""
    for name in (stop.get("name_cn"), stop.get("name_ja")):
        if name and (name in answer or (len(name) >= 3 and name[:3] in answer)):
            return True
    return False


def _language(expected: str, text: str) -> CheckResult:
    actual = detect_language(text)
    return CheckResult("answer.language", actual == expected, "" if actual == expected else f"期望 {expected}，实际 {actual}")


def detect_language(text: str) -> str:
    """粗略判断：拉丁字母比汉字 + 假名多 → en；假名多 → ja；否则 zh。

    中文回答里也会夹日文地名（多是汉字）和少量片假名，所以 ja 要求假名占比明显。
    先判 en：实测英文回答里每个地点都括注日文原名（「Suga Shrine (須賀神社 男坂)」），
    假名占 CJK 字符的比例很高，先判 ja 会误判
    """
    kana = len(re.findall(r"[぀-ヿ]", text))
    han = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if latin > han + kana:
        return "en"
    if kana >= 10 and kana >= 0.25 * (kana + han):
        return "ja"
    return "zh"


def _check(name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(name, passed, "" if passed else detail)
