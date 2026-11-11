# 执行失败与库外拒答修复回归（2026-10-21）

本步骤已完成：修复未执行来源查询却显示“资料不足”的状态错误，并对正式报告中12条原失败问题各做一次当前生产链路重放。8道库内题均正常交付，4道库外题均明确拒答，无执行失败或降级；独立Qwen模型对12条回答的正确性、完整性和引用支持均判通过。

这是**已消费题的修复回归，不是新的封存测试，也不是整份PRD验收通过**。原正式报告及6通过、6未达标、2待验收的结论保持不变。

## 原因与修改

原正式报告`20261020T084856210072Z/report.json`的生产Agent共12条：

| 原因 | 条数 | 原始表现 |
|---|---:|---|
| 多余的第二次规划遇到HTTP429 | 1 | 食材工具已交付原文，但最终Agent为partial |
| 熔断 | 10 | 未交付工具结果，agent_unavailable |
| 网络连接失败或超时 | 1 | 未交付工具结果，agent_unavailable |

四道库外题当时均没有完成来源查询，所以旧拒答指标0/4包含服务不可用，不能据此认定模型在成功查询后编造了四份配方。原失败仍计入正式端到端指标，不因重新分类而删除。

已存在的问答完成边界避免完整工具结果后的多余规划，本次确认原食材题不再请求第二次规划。当前仍发现并复现另一处问题：经典Agent没有选择工具时返回`insufficient`，`outputs=[]`、`error=null`，会把规划未执行误标成资料不足；多Agent没有调用来源工具也存在同样状态分支。

本次仅修改两处最终状态：

- 经典Agent无工具输出时返回`agent_unavailable`，保留`no_tool_selected`停止原因，明确提示“规划未调用来源工具，尚未完成知识库查询，请重试”。
- 多Agent无工具输出时同样返回`agent_unavailable`及明确执行错误。
- 规划自由文字不作为菜谱回答或拒答；已执行来源工具后返回的真实`insufficient`保持资料不足；429、熔断、取消、预算、工具异常及来源版本冲突继续明确失败或降级。

文件：`scripts/recipe_agent.py`、`scripts/langchain_multi_agent.py`。故障边界测试见`tests/test_failure_refusal_boundary.py`及两个Agent测试文件。无工具分支的修改前复现与修改后结果单独保存在`no-tool-boundary-reproduction.json`，明确标为模型决策注入，不冒充真实API成绩。

## 实际验证

入口：`scripts/replay_execution_failures.py`。固定从原正式报告选择执行未完成的12条生产Agent问题，每题只运行一次；校验历史来源哈希，保留原问题和原始输出，使用生产共享准入、熔断及冷却策略，未提高RPM/TPM或重置运行库来通过测试。

证据目录：`data/processed/prd-acceptance/failure-recovery-20261021/`。

| 检查 | 实测结果 |
|---|---|
| 当前真实生产链路重放 | 12/12完成，8道库内正常交付、4道库外明确拒答，0执行失败、0降级 |
| 规划调用数 | 每题1次，12题均未追加多余规划 |
| 独立judge | Qwen/Qwen3.5-35B-A3B，12/12正确、完整、引用支持；没有替代真人签字 |
| 引用规则 | 17/17菜名、章节、路径、行范围及逐字摘录通过 |
| 后端完整回归 | 644项通过，45.206秒，无跳过；开启真实PostgreSQL/Milvus集成 |
| 前端回归 | 25项通过，无跳过 |
| 环境和应用 | 应用已重启、HTTP200，活动集合`recipe_bounded_v4_metadata`，372来源、2074块，实时环境检查通过 |
| 可复核性 | 生成阶段代码哈希前后一致；生成报告先冻结，独立评审后12条原回答不变 |

故障注入覆盖未选择工具、HTTP429、熔断、预算耗尽、取消、检索步骤失败、来源版本冲突，以及真实资料不足与原文候选降级的区分。这些是可重复的本地边界测试；本次真实重放没有出现429，不能据此宣称供应商限流已永久消失。

描述性P50约5.63秒，P95约18.84秒，未作同条件基线时延对照，也不证明生产SLA。12/12独立judge通过的Wilson 95%区间约75.8%–100%；4/4库外拒答约51.0%–100%，小样本不推断全库质量。

实际产品HTTP调用39次，其中7次无用量；独立judge另12次。费用、真实账号RPM/TPM及用户预算仍未知，不计零成本。

原始执行和评分见`report.json`，评分前快照为`generation-report.json`，汇总为`summary.json`。本步骤不重跑完整消融矩阵、不覆盖旧正式成绩；后续生产时延、独立新封存测试、人评与成本的验收仍按各自要求进行。

复验命令（输出目录必须不存在）：

```sh
python scripts/replay_execution_failures.py --out data/processed/prd-acceptance/新回归目录
python scripts/review_prd_eval.py data/processed/prd-acceptance/新回归目录/report.json --judge
COOKBUDDY_INTEGRATION_TESTS=1 python -m unittest discover -s tests
node --test tests/test_answer_rendering.cjs
```
