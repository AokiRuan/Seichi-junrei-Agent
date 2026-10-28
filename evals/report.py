"""把逐条结果汇总成指标、打印报告、对比两次运行。讲解见 docs/16-evaluation.md。

    python -m evals.report evals/results/A.json               # 重新打印一次运行的报告
    python -m evals.report evals/results/A.json B.json        # 对比两次运行（B 相对 A）

指标：
- 交付率：跑完并给出了回答（TravelPlanner 的 Delivery Rate）
- 通过率 pass^1：一次运行里所有检查全过的比例（TravelPlanner 的 Final Pass Rate）
- pass^k：同一条用例跑 k 次，k 次全过的概率（τ-bench）。衡量稳定性：
  用户不会因为「十次里对了七次」而满意，他只会碰到错的那三次
- 检查通过率：所有单项检查里通过的比例（TravelPlanner 的 micro pass rate），比 pass^1 细
- 无效工具调用率：出错的调用 + 同一轮里参数完全相同的重复调用，占全部调用的比例
- 成本：平均 token、延迟、工具调用次数、LLM 轮数
- infra_retries：LLM 接口出错而重跑的次数（不影响分数，但太多说明网络或服务商有问题）
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from typing import Any

from backend.agent.harness import Harness

from .records import Trial


def summarize(results: list[dict[str, Any]], faulted: dict[str, set[str]]) -> dict[str, Any]:
    """results：每项是 {case_id, category, trial(dict), checks[...], passed}。faulted：用例 → 被注入故障的工具。"""
    trials = [Trial.from_dict(r["trial"]) for r in results]
    by_case: dict[str, list[bool]] = defaultdict(list)
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_case[r["case_id"]].append(r["passed"])
        by_cat[r["category"]].append(r["passed"])
    k = min(len(v) for v in by_case.values())

    checks = [c for r in results for c in r["checks"]]
    failed = [c["name"] for c in checks if not c["passed"]]
    latencies = sorted(t.latency_s for t in trials)

    calls = invalid = 0
    for r, t in zip(results, trials):
        for turn in t.turns:
            seen: Counter[str] = Counter()
            for c in turn.calls:
                if c.name in faulted.get(r["case_id"], set()):
                    continue  # 故意注入的失败不算 agent 的无效调用
                calls += 1
                key = Harness.call_key(c.name, c.args)
                seen[key] += 1
                if not c.ok or seen[key] > 1:
                    invalid += 1

    return {
        "cases": len(by_case),
        "trials_per_case": k,
        "delivery_rate": _mean(c["passed"] for c in checks if c["name"] == "delivery"),
        "pass_rate": _mean(r["passed"] for r in results),
        f"pass^{k}": pass_hat_k(by_case, k),
        "check_pass_rate": _mean(c["passed"] for c in checks),
        "invalid_tool_call_rate": invalid / calls if calls else 0.0,
        "avg_tokens": statistics.mean(t.total_tokens for t in trials),
        "avg_latency_s": statistics.mean(latencies),
        "p90_latency_s": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))],
        "avg_tool_calls": statistics.mean(len(t.tool_calls) for t in trials),
        "avg_iterations": statistics.mean(t.iterations for t in trials),
        "infra_retries": sum(t.infra_retries for t in trials),
        "by_category": {cat: _mean(v) for cat, v in sorted(by_cat.items())},
        "failures_by_type": dict(Counter(n.split(".")[0] for n in failed).most_common()),
        "top_failed_checks": dict(Counter(failed).most_common(10)),
    }


def pass_hat_k(by_case: dict[str, list[bool]], k: int) -> float:
    """τ-bench 的无偏估计：每条用例跑 n 次、成功 c 次，pass^k = C(c,k)/C(n,k)，再对用例求平均。"""
    return _mean(comb(sum(v), k) / comb(len(v), k) for v in by_case.values())


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


# ------------------------------------------------------------------ 打印

PCT_KEYS = ("delivery_rate", "pass_rate", "check_pass_rate", "invalid_tool_call_rate")


def _fmt(key: str, value: Any) -> str:
    if key in PCT_KEYS or key.startswith("pass^"):
        return f"{value:.1%}"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def print_report(run: dict[str, Any]) -> None:
    meta, s = run["meta"], run["summary"]
    print(f"\n== {meta['label']} · {meta['model']} · commit {meta['git_commit']} · {meta['started_at']}")
    for key, value in s.items():
        if not isinstance(value, dict):
            print(f"  {key:24} {_fmt(key, value)}")
    print("  按类别通过率   " + "  ".join(f"{c} {v:.0%}" for c, v in s["by_category"].items()))
    print("  失败类型       " + ("  ".join(f"{c}×{n}" for c, n in s["failures_by_type"].items()) or "无"))

    print("\n  用例                          通过   失败的检查")
    for case_id, rows in _group(run["results"]).items():
        passed = sum(r["passed"] for r in rows)
        fails = Counter(c["name"] for r in rows for c in r["checks"] if not c["passed"])
        print(f"  {case_id:30}{passed}/{len(rows)}   " + ", ".join(f"{n}×{c}" if c > 1 else n for n, c in fails.items()))
    print()
    for r in run["results"]:
        for c in r["checks"]:
            if not c["passed"]:
                print(f"  ✗ {r['case_id']}#{r['trial']['trial']} {c['name']}: {c['detail'][:160]}")


def _group(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        out[r["case_id"]].append(r)
    return out


def compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    print(f"\n== {a['meta']['label']} → {b['meta']['label']}")
    for key, va in a["summary"].items():
        vb = b["summary"].get(key)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            print(f"  {key:24} {_fmt(key, va):>8} → {_fmt(key, vb):>8}   ({vb - va:+.3g})")
    ga, gb = _group(a["results"]), _group(b["results"])
    print("\n  通过次数变化的用例：")
    changed = False
    for case_id in sorted(set(ga) & set(gb)):
        pa, pb = (sum(r["passed"] for r in g[case_id]) / len(g[case_id]) for g in (ga, gb))
        if pa != pb:
            changed = True
            print(f"  {'↑' if pb > pa else '↓'} {case_id:30} {pa:.0%} → {pb:.0%}")
    if not changed:
        print("  （无）")


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    runs = [load(p) for p in sys.argv[1:]]
    if len(runs) == 1:
        print_report(runs[0])
    elif len(runs) == 2:
        compare(*runs)
    else:
        sys.exit("用法：python -m evals.report A.json [B.json]")
