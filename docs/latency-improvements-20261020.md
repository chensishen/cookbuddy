# 调用链与评测改进验收（2026-10-20）

本轮代码修改与自动复验完成，**完整PRD仍为部分通过**。535项Python回归与25项前端测试全部通过。最终10题主集Agent P95为4.654秒，固定工作流为3.405秒；三道通用生成题Agent P95为9.520秒，固定工作流为5.853秒，后者仍存在明显的语义复核开销。

## 落实的修改

- **重排最多排队0.5秒。** `RAG_RERANK_ADMISSION_SECONDS`可设置0.05至8秒，API阶段总预算仍为8秒；TPM/RPM/并发/半开探测等待分别记录。排队不足时保留原候选顺序，父请求取消和总预算耗尽继续终止，不提高准入额度。事件表兼容升级，每题留存准入、API、规划和工具耗时。
- **明确单目标直接路由。** 唯一菜谱事实问题或明确库存查询跳过意图模型与意图向量，交给原菜谱工具并保留全文、硬限制和来源核验。原文配图、多菜、历史记忆与复杂目标仍走原路由，自定义目标仍受注册表约束。显式Agent模式保留真实规划调用，不伪装为零调用Agent。
- **两项语义复核默认关闭思考。** 原实现显式启用思考，糖醋汁完整性检查曾耗时13.179秒、包含1025个reasoning token。默认改为非思考JSON，仍分别进行支持性和完整性检查、保留原通过条件与严格解析。`RECIPE_REVIEW_THINKING=1`保留作对照，未删除检查以换取提速。
- **默认重排上限为10。** 按开发集10/20/30对照选择，保留 `RAG_RERANK_CANDIDATES=1..50`。调整的是重排候选数，知识库、召回范围与最终Top-5未缩小为10道菜。
- **评测缓存隔离与顺序交替。** 按运行/题目/方案隔离本地Embedding与重排缓存，Agent和工作流交替执行，共享本轮准入额度。记录实际回答方法，区分规则快速路径与模型生成；代码指纹覆盖 `scripts/**/*.py` 路径与内容，配置快照记录复核模式及准入限额。

## 最终验证范围

535项Python测试全部通过，无跳过，耗时77.669秒，包含真实PostgreSQL/Milvus；25项前端测试通过。新增12项测试覆盖限流原因、短排队预算、取消、旧表升级、缓存隔离、简单路由边界、交替顺序、子目录指纹、规划/工具耗时和复核JSON参数。五个容器与372份来源、2074块的活动集合运行检查正常。模型、向量空间与活动索引保持原配置，未重建知识库。

最终两组在线评测在回归结束后串行执行，均退出0，期间脚本未变化，报告指纹与当前代码一致。共32组回答、32组judge、24组Ragas忠实度评分。没有启动应用进行浏览器验收；本轮不宣称生产SLA或首token指标通过。

- [回归日志](../data/processed/prd-acceptance/latency-improvements-20261020/tests.log)、[前端测试日志](../data/processed/prd-acceptance/latency-improvements-20261020/frontend-tests.log)。
- [执行命令与报告路径](../data/processed/prd-acceptance/latency-improvements-20261020/runs.json)、[配置、指纹和退出码](../data/processed/prd-acceptance/latency-improvements-20261020/verification.json)。

## 最终10题主集

沿用原开发诊断题：8题有检索gold，5题分别做四方案回答。不是独立盲测。

| 方案 | Judge正确完整 | Ragas忠实度均值 | P50 / P95秒 |
|---|---:|---:|---:|
| closed_book | 4/5 | 0.389 | 1.592 / 2.493 |
| naive_rag | 4/5 | 1.000 | 1.619 / 4.678 |
| full_agent | 5/5 | 0.938 | 1.466 / 4.654 |
| fixed_workflow | 5/5 | 0.979 | 0.122 / 3.405 |

[原始逐题记录与分阶段事件](../data/processed/prd-acceptance/20261020T053512854911Z/report.json)、[评测对照](../data/processed/prd-acceptance/20261020T053512854911Z/report.md)、[执行日志](../data/processed/prd-acceptance/latency-improvements-20261020/main-eval.log)、[门槛表](../data/processed/prd-acceptance/20261020T053512854911Z/acceptance.md)。

完整系统Hit@5为100%，MRR由不加重排的0.8333升至0.9375，真实重排8/8，无该批重排降级；配对检验p=0.5，**仍不显著**。来源还原372/372通过；完整Agent与固定工作流库外拒答均2/2，Naive为1/2。

主集Agent样本P95为4.654秒，比本轮Naive的4.678秒略低，比固定工作流高1.250秒。此处达到本轮1.5秒增量建议，但五题样本及供应商波动不足以证明长期SLA。Ragas每方案仅评分三道库内题，非独立准确率认证。

库存题Agent与固定工作流的回答和引用完全相同，Ragas分别为0.8125和0.9375；扣分理由包括将“未说明是否有牛奶”判为与“库存未知”冲突，以及否定原文中已出现的可选搅拌器。保留原始评分与断言理由，不自行改成满分，仍需独立人评校准。

## 三道通用生成题

新增[跨步骤开发题](../data/evaluation/prd-generation-dev-v1.json)：蒸水蛋的盐与搅拌、糖醋汁的烹制收汁、土豆丝的分次加蒜。gold与来源哈希按当前原文构造，不称为盲测。

| 方案 | Judge正确完整 | Ragas忠实度均值 | P50 / P95秒 |
|---|---:|---:|---:|
| closed_book | 1/3 | 0.337 | 1.882 / 2.121 |
| naive_rag | 1/3 | 0.792 | 1.615 / 2.381 |
| full_agent | 3/3 | 1.000 | 8.466 / 9.520 |
| fixed_workflow | 3/3 | 1.000 | 5.519 / 5.853 |

[最终逐题记录](../data/processed/prd-acceptance/20261020T053850137559Z/report.json)、[对照与延迟](../data/processed/prd-acceptance/20261020T053850137559Z/report.md)、[执行日志](../data/processed/prd-acceptance/latency-improvements-20261020/generation-eval.log)。完整Agent与工作流的六组回答均实际执行生成、支持性复核、完整性复核，全部核验通过；不是用快速路径代替生成测试。

三题Agent P95为9.520秒，Naive为2.381秒，增量7.139秒；工作流为5.853秒，增量3.472秒，**通用生成仍未达到1.5秒增量建议**。两个独立检查及网络往返有真实代价，不用简单题成绩掩盖。

复核模式对照同样保留：[思考复核、30候选](../data/processed/prd-acceptance/20261020T052233341280Z/report.json)的Agent/工作流P95为18.487/17.051秒；[非思考复核、30候选](../data/processed/prd-acceptance/20261020T052559739834Z/report.json)为7.593/5.809秒，且两轮judge均3/3、Ragas均1.0。关闭思考后两项复核API约1.2–1.7秒，返回的复核reasoning token为0。最终默认10候选使用上表的独立复测，未挑选更快的中间轮次替代最终结果。小样本、不同输出和供应商波动限制了因果归因；开关保留，不能保证所有复杂题都无需思考。

## 参数与故障对照

| 候选上限 | Hit@5 | MRR | 实际重排 | 阶段P50秒 | 样本P95秒 |
|---|---:|---:|---:|---:|---:|
| 10 | 100% | 0.9375 | 8/8 | 0.742 | 0.860 |
| 20 | 100% | 0.9167 | 8/8 | 0.990 | 1.213 |
| 30 | 100% | 0.9062 | 7/8 | 1.320 | 2.003 |

[参数对照](../data/processed/prd-acceptance/rerank-sweep-20261020.json)：固定既有8题，顺序10→20→30，各组独立运行库。30候选的噪声题因TPM不足降级，10/20两组无降级。阶段耗时包含脚本启动与本地处理；属于开发调优，不证明全库最优。

[离线TPM故障注入](../data/processed/prd-acceptance/admission-comparison-20261020.json)：同样额度耗尽，等待由8.097秒降为0.507秒，原候选顺序均保留，未调用供应商。[真实注册表路由验证](../data/processed/prd-acceptance/local-routing-20261020.json)：两条明确查询约0.033/0.009秒，意图模型与向量调用均为0；该数字仅是路由耗时。

有一轮将回归与性能测量并行，延迟会受本机负载干扰；[过程记录](../data/processed/prd-acceptance/20261020T053042694923Z/measurement-conditions.json)已保留，最终性能使用回归结束后的独立串行复测。本地缓存隔离不等于关闭供应商侧缓存。

## 验收结论与复跑

主集门槛6项通过、1项未达标、6项待验收，严格命令按预期返回2。剩余未达标是重排统计显著性；补充集另外暴露通用生成延迟未达标。独立测试、人评与judge一致性、98%语义引用准确率、完整消融和固定512-token基线、其余Ragas指标、完整费用及预算仍待完成。两个数据范围不混合计算一个总体准确率；同模型评审不代替两位独立人工评审。

费用继续留空：当前模型/供应商与旧价格快照不匹配，Embedding、重排、失败、评审等费用不完整。已记录token只能用于调用量比较，不是总账。

```sh
# 先完成回归，再测性能。
COOKBUDDY_INTEGRATION_TESTS=1 /opt/anaconda3/envs/py3.12.11/bin/python -m unittest discover -s tests
node --test tests/test_answer_rendering.cjs
make eval PYTHON=/opt/anaconda3/envs/py3.12.11/bin/python
/opt/anaconda3/envs/py3.12.11/bin/python scripts/evaluate_prd_suite.py --fixture data/evaluation/prd-generation-dev-v1.json
```

当前`latest.json`默认验收入口保留最终10题主集，同时列出三题补充报告；`make eval-acceptance`仍会返回非零，不能以评测执行完成冒充PRD全部达标。下一次运行会按实际生成的新报告更新入口。
