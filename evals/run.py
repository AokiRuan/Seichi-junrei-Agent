"""评测入口：不经过 HTTP，直接驱动 AgentLoop 跑完整个任务集。讲解见 docs/16-evaluation.md。

    python -m evals.run                              # 全部用例各跑 1 次（会消耗 LLM token）
    python -m evals.run --repeat 3 --label baseline  # 每条跑 3 次，算 pass^3
    python -m evals.run --only plan-bocchi-shinjuku fault-geocode-down
    python -m evals.run --max-iterations 3 --label iter3   # 消融：改 harness 参数再跑
    python -m evals.run --rescore evals/results/xxx.json   # 改了检查规则后对旧结果重新打分，不花 token

结果写到 evals/results/<时间>-<label>.json，用 python -m evals.report 查看或对比。

组装和 backend/app/components.py 一样（真实 LLM + 真实 MCP server），区别只有：
- system prompt 用固定的「当前时间」（cases.EVAL_NOW）
- 工具外面套了缓存和故障注入（evals/tools.py）
- 默认不上报 Langfuse（--trace 打开），免得评测把线上 trace 冲乱
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.agent.harness import Harness
from backend.agent.llm import ChatModel, LLMClient
from backend.agent.loop import AgentLoop
from backend.agent.mcp_client import MCPClientPool, ToolExecutor
from backend.agent.prompts import SYSTEM_PROMPT_TEMPLATE, build_system_prompt
from backend.agent.tracing import Tracing
from backend.app.config import settings
from backend.mcp_servers.seichi.anitabi import SNAPSHOT_FILE

from .cases import EVAL_NOW, Case, load_cases
from .checks import run_checks
from .records import ToolCallRecord, Trial, TurnRecord
from .report import load, print_report, summarize
from .tools import CachedTools, FaultyTools

RESULTS_DIR = Path(__file__).parent / "results"


async def run_trial(
    case: Case, n: int, llm: ChatModel, tools: ToolExecutor, harness: Harness, tracing: Tracing, run_id: str
) -> Trial:
    if case.faults:
        tools = FaultyTools(tools, case.faults)
    loop = AgentLoop(llm=llm, tools=tracing.wrap_tools(tools), harness=harness)
    trial = Trial(case_id=case.id, trial=n, trace_session=f"eval-{run_id}-{case.id}-{n}" if tracing.enabled else None)
    messages: list[dict[str, Any]] = [{"role": "system", "content": build_system_prompt(EVAL_NOW)}]
    started = time.perf_counter()
    for user in case.turns:
        messages.append({"role": "user", "content": user})
        turn = TurnRecord(user=user)
        trial.turns.append(turn)
        pending: dict[str, ToolCallRecord] = {}
        with tracing.turn(session_id=trial.trace_session, user_input=user) as span:
            async for event in loop.run(messages):
                if event.type == "token":
                    turn.answer += event.content or ""
                elif event.type == "tool_call":
                    pending[event.tool_call_id] = rec = ToolCallRecord(name=event.tool_name, args=event.payload or {})
                    turn.calls.append(rec)
                elif event.type == "tool_result":
                    rec = pending[event.tool_call_id]
                    rec.is_error, rec.data, rec.error = event.payload["is_error"], event.payload["data"], event.payload.get("error")
                elif event.type == "done":
                    messages += event.payload["new_messages"]
                    turn.iterations, turn.usage = event.payload["iterations"], event.payload["usage"]
                    turn.answer = _final_answer(event.payload["new_messages"])
                    span.finish(turn.answer, iterations=turn.iterations, usage=turn.usage)
                elif event.type == "error":
                    turn.error = event.message
                    span.fail(event.message or "")
        if turn.error:
            break  # 和真实服务一样：出错的这一轮不进历史，后面的轮次也没法继续
    trial.latency_s = round(time.perf_counter() - started, 2)
    return trial


# loop 在 LLM 调用失败时产出的 error 事件以这些开头（见 loop.py）
INFRA_ERRORS = ("LLM 调用失败", "LLM 流式响应意外结束")


async def run_with_retry(case: Case, n: int, retries: int, **kw: Any) -> Trial:
    """LLM 接口超时 / 断连时整条重跑。

    实测（docs/16）：一次 50 条的运行里有 4 条因为 DeepSeek 接口超时失败，和改动无关，
    却让交付率从 100% 掉到 92%，把对比结果搅乱了。评测要测的是 agent，不是网络
    """
    for attempt in range(retries + 1):
        trial = await run_trial(case, n, **kw)
        trial.infra_retries = attempt
        if not (trial.error and trial.error.startswith(INFRA_ERRORS)):
            break
    return trial


def _final_answer(new_messages: list[dict[str, Any]]) -> str:
    """最后一条有文字的 assistant 消息。token 流里还混着工具调用前的「我来查一下」，不算回答。"""
    for m in reversed(new_messages):
        if m["role"] == "assistant" and m.get("content"):
            return m["content"]
    return ""


def grade(cases: dict[str, Case], trials: list[Trial]) -> list[dict[str, Any]]:
    results = []
    for t in trials:
        checks = run_checks(cases[t.case_id], t)
        results.append({
            "case_id": t.case_id,
            "category": cases[t.case_id].category,
            "passed": all(c.passed for c in checks),
            "checks": [asdict(c) for c in checks],
            "trial": asdict(t),
        })
    return results


async def run_all(args: argparse.Namespace, cases: list[Case]) -> tuple[list[Trial], dict[str, Any]]:
    tracing = Tracing.from_settings(settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_host) \
        if args.trace else Tracing(None)
    llm = tracing.wrap_llm(LLMClient(args.model, settings.llm_api_key, settings.llm_base_url), args.model)
    harness = Harness(
        max_iterations=args.max_iterations,
        max_repeat_calls=args.max_repeat_calls,
        max_tool_failures=args.max_tool_failures,
    )
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    try:
        async with MCPClientPool() as pool:
            tools: ToolExecutor = pool
            # 工具描述也是 prompt 的一部分：改了 docstring，LLM 的行为就可能变
            tools_sha = _sha(pool.tool_schemas())
            cache = None if args.no_cache else CachedTools(pool)
            if cache:
                tools = cache

            async def one(case: Case, n: int) -> Trial:
                nonlocal done
                async with sem:
                    trial = await run_with_retry(
                        case, n, args.infra_retries,
                        llm=llm, tools=tools, harness=harness, tracing=tracing, run_id=run_id,
                    )
                done += 1
                status = "✗ " + trial.error if trial.error else f"{trial.latency_s}s {trial.total_tokens} tokens"
                print(f"  [{done}/{len(cases) * args.repeat}] {case.id}#{n} {status}", flush=True)
                return trial

            trials = await asyncio.gather(*(one(c, n) for c in cases for n in range(1, args.repeat + 1)))
            if cache:
                cache.save()
    finally:
        tracing.shutdown()

    meta = {
        "run_id": run_id,
        "label": args.label,
        "started_at": run_id,
        "model": args.model,
        "git_commit": _git_commit(),
        "prompt_sha": _sha(SYSTEM_PROMPT_TEMPLATE),
        "tools_sha": tools_sha,
        "tools_fingerprint": cache.fingerprint if cache else None,
        "cache": {"hits": cache.hits, "misses": cache.misses} if cache else None,
        "harness": {
            "max_iterations": args.max_iterations,
            "max_repeat_calls": args.max_repeat_calls,
            "max_tool_failures": args.max_tool_failures,
        },
        "eval_now": EVAL_NOW.isoformat(),
        "repeat": args.repeat,
    }
    return list(trials), meta


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except OSError:
        return "unknown"


def save(meta: dict[str, Any], cases: dict[str, Case], trials: list[Trial]) -> tuple[Path, dict[str, Any]]:
    results = grade(cases, trials)
    faulted = {c.id: set(c.faults) for c in cases.values()}
    run = {"meta": meta, "summary": summarize(results, faulted), "results": results}
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{meta['run_id']}-{meta['label']}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, run


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="python -m evals.run")
    p.add_argument("--only", nargs="*", help="只跑这些用例 id")
    p.add_argument("--category", nargs="*", help="只跑这些类别")
    p.add_argument("--repeat", type=int, default=1, help="每条用例跑几次（用于 pass^k）")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--label", default="run")
    p.add_argument("--model", default=settings.llm_model)
    p.add_argument("--max-iterations", type=int, default=settings.max_iterations)
    p.add_argument("--max-repeat-calls", type=int, default=2)
    p.add_argument("--max-tool-failures", type=int, default=2, help="0 关闭工具熔断（消融用）")
    p.add_argument("--infra-retries", type=int, default=2, help="LLM 接口超时 / 断连时整条重跑几次")
    p.add_argument("--no-cache", action="store_true", help="工具全部实时调用，不读也不写缓存")
    p.add_argument("--trace", action="store_true", help="上报 Langfuse（每次运行一个 session）")
    p.add_argument("--rescore", metavar="RESULT_JSON", help="对已有结果按当前规则重新打分")
    args = p.parse_args(argv)

    cases = load_cases()
    by_id = {c.id: c for c in cases}

    if args.rescore:
        old = load(args.rescore)
        trials = [Trial.from_dict(r["trial"]) for r in old["results"]]
        meta = {**old["meta"], "label": old["meta"]["label"] + "-rescored", "rescored_from": args.rescore}
        path, run = save(meta, by_id, trials)
        print_report(run)
        print(f"结果：{path}")
        return

    if args.only:
        unknown = set(args.only) - set(by_id)
        if unknown:
            sys.exit(f"没有这些用例：{sorted(unknown)}")
        cases = [by_id[i] for i in args.only]
    if args.category:
        cases = [c for c in cases if c.category in args.category]

    # 评测必须用真实快照：mock 只有 5 部作品，用例里的 anime_id 都对不上。
    # 环境变量要在启动 MCP 子进程之前设好（经 PASSTHROUGH_ENV 传进去）
    if not SNAPSHOT_FILE.exists():
        sys.exit("没有 Anitabi 快照，先执行 python -m backend.mcp_servers.seichi.anitabi")
    os.environ["SEICHI_DATA_SOURCE"] = "anitabi"

    print(f"运行 {len(cases)} 条用例 × {args.repeat} 次，并发 {args.concurrency}，模型 {args.model}")
    trials, meta = asyncio.run(run_all(args, cases))
    path, run = save(meta, by_id, trials)
    print_report(run)
    print(f"结果：{path}")


if __name__ == "__main__":
    main(sys.argv[1:])
