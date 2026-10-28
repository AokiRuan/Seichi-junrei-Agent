"""评测框架本身的测试：用例校验、检查规则、指标、缓存与故障注入。讲解见 docs/16-evaluation.md。

评测是用来判断别的代码对不对的，它自己错了最难发现（检查永远通过）——所以也要测。
"""
from dataclasses import asdict
from datetime import datetime

import pytest

from backend.agent.harness import Harness
from backend.agent.mcp_client import ToolOutcome
from backend.agent.prompts import JST
from backend.agent.tracing import Tracing
from backend.tests.fakes import FakeLLM, FakeTools, answer, call, with_tools
from evals.cases import Case, _parse, load_cases, resolve_date
from evals.checks import detect_language, match, plan_validity, run_checks
from evals.records import ToolCallRecord, Trial, TurnRecord
from evals.report import pass_hat_k, summarize
from evals.run import grade, run_trial, run_with_retry
from evals.tools import CachedTools, FaultyTools

WED = datetime(2026, 10, 28, 9, 0, tzinfo=JST)


def test_all_cases_are_valid():
    cases = load_cases()
    assert len(cases) >= 20
    assert len({c.category for c in cases}) >= 6


def test_typo_in_expect_is_rejected():
    # 写错的检查键如果被静默忽略，那条检查就永远「通过」
    with pytest.raises(ValueError, match="must_cal"):
        _parse({"id": "x", "category": "c", "turns": ["q"], "expect": {"tools": {"must_cal": ["a"]}}}, WED)
    with pytest.raises(ValueError, match="matcher"):
        _parse({"id": "x", "category": "c", "turns": ["q"], "expect": {"args": {"t": {"a": {"between": [1, 2]}}}}}, WED)


def test_resolve_date():
    assert resolve_date("+1", WED) == "20261029"
    assert resolve_date("sat", WED) == "20261031"
    assert resolve_date("wed", WED) == "20261104"  # 今天是周三时，「周三」指下周三
    case = _parse({"id": "x", "category": "c", "turns": ["q"],
                   "expect": {"args": {"plan_pilgrimage": {"date": "@date:sat"}}}}, WED)
    assert case.expect["args"]["plan_pilgrimage"]["date"] == "20261031"


def test_match():
    args = {"anime_id": 1, "budget": 240, "area_id": None}
    assert match(args, "anime_id", 1)
    assert not match(args, "anime_id", 2)
    assert match(args, "anime_id", {"one_of": [1, 3]})
    assert match(args, "budget", {"range": [180, 300]})
    assert not match(args, "budget", {"range": [0, 100]})
    assert match(args, "area_id", {"present": False})  # 传了 null 等于没传
    assert match(args, "missing", {"present": False})
    assert not match(args, "missing", {"range": [0, 1]})


def _plan(**over):
    plan = {
        "budget_min": 120, "total_min": 70, "area": {"id": "a1"}, "start": {"lat": 35.0, "lng": 139.0},
        "access": {"duration_min": 20},
        "stops": [
            {"order": 1, "spot_id": "s1", "name_cn": "下北泽站", "dwell_min": 15, "walk_from_prev_min": 0, "arrive_at": "10:20"},
            {"order": 2, "spot_id": "s2", "name_ja": "下北沢南口", "dwell_min": 15, "walk_from_prev_min": 20, "arrive_at": "10:55"},
        ],
        "assumptions": [],
    }
    return {**plan, **over}


def test_plan_validity_passes_consistent_plan():
    assert all(c.passed for c in plan_validity(_plan()))


def test_plan_validity_catches_broken_plans():
    def failed(plan):
        return {c.name for c in plan_validity(plan) if not c.passed}

    assert "plan.budget" in failed(_plan(total_min=130))
    stops = _plan()["stops"]
    assert "plan.unique_stops" in failed(_plan(stops=[stops[0], {**stops[1], "spot_id": "s1"}]))
    assert "plan.clock" in failed(_plan(stops=[stops[0], {**stops[1], "arrive_at": "10:00"}]))
    assert "plan.walk_legs" in failed(_plan(stops=[stops[0], {**stops[1], "walk_from_prev_min": 45}], total_min=95))
    assert "plan.total_consistent" in failed(_plan(total_min=100))


def test_detect_language():
    assert detect_language("孤独摇滚的圣地主要在下北泽，共 41 处。") == "zh"
    assert detect_language("ぼっち・ざ・ろっく！の聖地は下北沢にあります。ぜひ行ってみてください。") == "ja"
    assert detect_language("The main locations are around Shimokitazawa, 下北沢.") == "en"
    # 英文回答里每个地点括注日文原名，假名比例很高，但仍是英文
    assert detect_language("Yotsuya Station (四ツ谷駅赤坂口) and Suga Shrine (須賀神社 男坂), "
                           "then the overpass (信濃町歩道橋) near Ladurée (ラデュレ 新宿店)") == "en"
    # 中文回答里夹几个片假名地名，不能判成日语
    assert detect_language("第一站是 STARRY（スターリー）所在的下北泽，然后去江之岛，走路大约十分钟就能到达。") == "zh"


def _trial(calls, text="下北泽站很值得去", turns_before=()):
    return Trial(case_id="x", trial=1, turns=[*turns_before, TurnRecord(user="q", answer=text, calls=calls)])


def test_run_checks_tools_args_plan_answer():
    case = Case(id="x", category="plan", turns=["q"], expect={
        "tools": {"must_call": ["search_anime", "plan_pilgrimage"], "must_not_call": ["plan_route"],
                  "order": ["search_anime", "plan_pilgrimage"]},
        "args": {"plan_pilgrimage": {"anime_id": 7, "time_budget_min": 120}},
        "plan": {"ok": True, "start_near": [35.0, 139.0, 1], "min_stops": 2},
        "answer": {"mentions_stops": 2, "not_contains": ["含往返"]},
    })
    calls = [
        ToolCallRecord("search_anime", {"title": "a"}, data=[{"anime_id": 7}]),
        ToolCallRecord("plan_pilgrimage", {"anime_id": 7, "time_budget_min": 120}, data=_plan()),
    ]
    results = run_checks(case, _trial(calls, "先去下北泽站，再去下北沢南口"))
    assert all(r.passed for r in results), [r for r in results if not r.passed]

    # 参数错、起点偏、回答写了坐标：分别被对应的检查抓到
    bad = [calls[0], ToolCallRecord("plan_pilgrimage", {"anime_id": 8},
                                    data=_plan(start={"lat": 35.1, "lng": 139.0}))]
    failed = {r.name for r in run_checks(case, _trial(bad, "坐标 35.6895, 139.6917")) if not r.passed}
    assert {"args.plan_pilgrimage", "plan.start_near", "answer.no_coordinates", "answer.mentions_stops"} <= failed


def test_not_contains_ignores_negated_phrases():
    case = Case(id="x", category="c", turns=["q"], expect={"answer": {"not_contains": ["含返程"]}})

    def passed(text):
        return next(r.passed for r in run_checks(case, _trial([], text)) if r.name == "answer.not_contains")

    assert passed("总时长包含去程交通，**不含返程**。")
    assert passed("不包含返程")
    assert not passed("总时长含返程交通")
    assert not passed("含返程。另外不含返程的话……")  # 只要有一处是肯定说法就算


def test_plan_ok_ignores_business_errors():
    case = Case(id="x", category="c", turns=["q"], expect={"plan": {"ok": True}})
    calls = [ToolCallRecord("plan_pilgrimage", {}, data={"error": "时间预算太少"})]
    [r] = [r for r in run_checks(case, _trial(calls)) if r.name == "plan.ok"]
    assert not r.passed and "时间预算太少" in r.detail


def test_area_changed_across_turns():
    case = Case(id="x", category="c", turns=["q1", "q2"], expect={"plan": {"area_changed": True}})
    first = TurnRecord(user="q1", answer="a", calls=[ToolCallRecord("plan_pilgrimage", {}, data=_plan())])
    same = [ToolCallRecord("plan_pilgrimage", {"area_id": "a1"}, data=_plan())]
    other = [ToolCallRecord("plan_pilgrimage", {"area_id": "a2"}, data=_plan(area={"id": "a2"}))]

    def passed(calls):
        return next(r.passed for r in run_checks(case, _trial(calls, turns_before=[first])) if r.name == "plan.area_changed")

    assert not passed(same)
    assert passed(other)


def test_delivery_failure_skips_other_checks():
    case = Case(id="x", category="c", turns=["q"], expect={"tools": {"must_call": ["a"]}})
    trial = Trial(case_id="x", trial=1, turns=[TurnRecord(user="q", error="LLM 调用失败")])
    assert [(r.name, r.passed) for r in run_checks(case, trial)] == [("delivery", False)]


def test_pass_hat_k():
    # 3 次全过 → 1；3 次过 2 次 → pass^1 = 2/3，pass^3 = 0
    assert pass_hat_k({"a": [True] * 3}, 3) == 1
    assert pass_hat_k({"a": [True, True, False]}, 1) == pytest.approx(2 / 3)
    assert pass_hat_k({"a": [True, True, False]}, 2) == pytest.approx(1 / 3)
    assert pass_hat_k({"a": [True, True, False], "b": [True] * 3}, 3) == 0.5


async def test_cached_tools(tmp_path):
    inner = FakeTools()
    path = tmp_path / "cache.json"
    cache = CachedTools(inner, path, fingerprint="v1")
    await cache.call_tool("search_anime", {"title": "a"})
    await cache.call_tool("search_anime", {"title": "a"})
    assert len(inner.calls) == 1 and (cache.hits, cache.misses) == (1, 1)
    cache.save()

    # 指纹相同：从文件读出来直接命中；指纹变了（工具代码改过）：作废重新调用
    again = CachedTools(inner, path, fingerprint="v1")
    await again.call_tool("search_anime", {"title": "a"})
    assert len(inner.calls) == 1
    changed = CachedTools(inner, path, fingerprint="v2")
    await changed.call_tool("search_anime", {"title": "a"})
    assert len(inner.calls) == 2


async def test_cached_tools_does_not_cache_errors(tmp_path):
    inner = FakeTools(lambda name, args: ToolOutcome(data=None, is_error=True, text="超时"))
    cache = CachedTools(inner, tmp_path / "c.json", fingerprint="v1")
    await cache.call_tool("geocode", {"query": "新宿"})
    await cache.call_tool("geocode", {"query": "新宿"})
    assert len(inner.calls) == 2


async def test_faulty_tools():
    inner = FakeTools()
    tools = FaultyTools(inner, {"geocode": "geocode 挂了"})
    bad = await tools.call_tool("geocode", {"query": "新宿"})
    good = await tools.call_tool("search_anime", {"title": "a"})
    assert bad.is_error and bad.text == "geocode 挂了"
    assert not good.is_error and inner.calls == [("search_anime", {"title": "a"})]


async def test_run_trial_end_to_end_with_fakes():
    """runner 正确收集多轮的调用、结果和回答；故障注入生效；结果能存成 JSON 再读回来重新打分。"""
    llm = FakeLLM(
        with_tools(call("c1", "search_anime", title="孤独摇滚"), content="我来查一下"),
        answer("找到了"),
        with_tools(call("c2", "geocode", query="新宿")),
        answer("出发地暂时查不到"),
    )
    case = Case(id="x", category="fault", turns=["想去孤独摇滚", "从新宿出发"], faults={"geocode": "超时"},
                expect={"tools": {"must_call": ["geocode"]}, "answer": {"contains_any": ["暂时"]}})
    trial = await run_trial(case, 1, llm, FakeTools(), Harness(), Tracing(None), "t")

    assert [t.answer for t in trial.turns] == ["找到了", "出发地暂时查不到"]  # 不含「我来查一下」
    assert trial.turns[1].calls[0].is_error and trial.turns[1].calls[0].error == "超时"
    # 第二轮请求带上了第一轮的完整历史（含工具消息）
    assert [m["role"] for m in llm.requests[2]["messages"]] == ["system", "user", "assistant", "tool", "assistant", "user"]

    [result] = grade({"x": case}, [trial])
    assert result["passed"], result["checks"]
    assert Trial.from_dict(asdict(trial)) == trial
    summary = summarize([result], {"x": {"geocode"}})
    assert summary["pass_rate"] == 1 and summary["invalid_tool_call_rate"] == 0  # 注入的失败不算无效调用


async def test_llm_timeouts_are_retried_not_scored():
    llm = FakeLLM(TimeoutError("Request timed out."), answer("好的"))
    case = Case(id="x", category="c", turns=["q"])
    trial = await run_with_retry(case, 1, 2, llm=llm, tools=FakeTools(), harness=Harness(),
                                 tracing=Tracing(None), run_id="t")
    assert trial.error is None and trial.last.answer == "好的" and trial.infra_retries == 1
