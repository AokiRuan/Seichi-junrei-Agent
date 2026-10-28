# evals

Agent 评测：固定任务集 + 规则检查 + 指标。讲解见 [docs/16-evaluation.md](../docs/16-evaluation.md)。

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m evals.run --repeat 2 --label baseline     # 全部用例各跑 2 次（消耗 LLM token）
python -m evals.run --only plan-bocchi-shinjuku     # 只跑某几条
python -m evals.report evals/results/A.json B.json  # 对比两次运行
python -m evals.run --rescore evals/results/A.json  # 改了检查规则后重新打分，不花 token
```

| 文件 | 作用 |
|---|---|
| `cases.yaml` | 任务集：问题、期望的工具调用 / 参数 / 行程 / 回答，故障注入 |
| `cases.py` | 读取并严格校验用例；固定评测时的「当前时间」 |
| `run.py` | 驱动 `AgentLoop` 跑用例，收集记录，打分，写结果 |
| `tools.py` | 工具响应缓存（`.cache/`）、故障注入 |
| `checks.py` | 规则检查：通用约束 + 用例约束 |
| `report.py` | 指标汇总（交付率、pass^1、pass^k、无效调用率、成本）、打印、对比 |

`results/` 和 `.cache/` 不进 git（缓存里有 Anitabi 数据）。评测要求本地有 Anitabi 快照。
