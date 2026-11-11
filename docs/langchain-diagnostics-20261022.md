# 重排降级诊断改进与实测

本轮落实诊断改进：保留现有模型、2秒重排预算、8秒检索工具预算、30秒请求预算和安全回退。没有用延长超时或成功复验覆盖失败，也未新增每轮评审模型。此前生产化验收保留为历史快照；本轮[汇总](../data/processed/langchain-diagnostics-20261022/summary.json)、[代码快照](../data/processed/langchain-diagnostics-20261022/manifest.json)独立保存。

## 已完成的改进

- 重排子进程及时写入不含正文的阶段记录，父进程在子进程超时或退出后仍可读取最后阶段和已观测耗时。临时记录由独立私有目录承载，正常退出或阶段异常后清理。
- 记录父进程准备及解释器启动、模块加载、数据读取、请求准备、准入排队、HTTP调用、收尾等耗时；缓存命中单独标识。被终止的HTTP阶段只表示已经等待多久，不代表完整接口耗时。
- 区分阶段预算耗尽、准入超时、429限流、熔断、网络错误、上游HTTP错误、结果校验失败及无法细分的子进程错误。缺乏证据时保留未知，不猜测供应商原因。
- 在线服务日志将重排回退记为`degraded`，附受限原因与阶段数据，不再把“进程成功返回回退结果”记作检索完全成功。
- 诊断字段采用固定枚举、数字及HTTP状态码白名单，不记录异常文本、请求头、API密钥、用户正文或原文内容。

代码入口：`scripts/stage_diagnostics.py`、`scripts/ask_test_recipes.py`、`scripts/rerank_test_recipes.py`、`scripts/service_runtime.py`、`scripts/langchain_service_worker.py`、`src/agent/runtime.py`。

## 本次真实故障已定位

7个在线场景中6个满足无降级条件，库外问题再次发生回退。这次[诊断记录](../data/processed/langchain-diagnostics-20261022/diagnosis.json)明确显示：

| 阶段 | 实际观测耗时 |
|---|---:|
| 父进程准备及解释器启动 | 22.557 ms |
| 模块加载 | 33.049 ms |
| 数据读取与校验 | 27.676 ms |
| 请求准备、缓存检查等 | 87.967 ms |
| 准入排队 | 1.093 ms |
| HTTP调用阶段，终止前已等待 | 1840.924 ms |
| 重排阶段总耗时，含超时回收 | 2013.267 ms |

阶段预算实际传入1999.068 ms，最后阶段为`api`，原因是`stage_deadline`。因此本次不是排队或数据加载占满预算，而是重排HTTP调用未在剩余预算内完成。HTTP阶段包含本地网络服务转发、网络及供应商处理，尚不能继续归因到其中某一项。历史那次只有合并错误码，不能倒推为已经确定相同原因。

本次回答仍明确资料不足，没有引用伪造来源。[在线报告](../data/processed/langchain-diagnostics-20261022/live/report.json)保留6/7的严格结果，不将安全拒答等同于无降级成功。

[独立计时探针](../data/processed/langchain-diagnostics-20261022/timing-probe.json)使用隔离缓存命名空间，对同一个查询先测缓存未命中，再连续测两次命中：首次重排950.518 ms，其中HTTP阶段696.565 ms；命中后分别197.674 ms和194.038 ms，均成功。共享配额、活动索引未修改。这只有3次顺序测量，不能代表冷机启动、延迟分位数或长期SLA。

## 验证与边界

- [相关回归](../data/processed/langchain-diagnostics-20261022/focused.log)：52项通过，包含5项新增诊断测试。覆盖真实子进程在HTTP阶段被超时终止、HTTP429、准入超时、错误类别、隐私白名单和服务降级状态。
- [完整后端](../data/processed/langchain-diagnostics-20261022/backend.log)：712项通过，含真实PostgreSQL/Milvus集成，无跳过。
- [前端](../data/processed/langchain-diagnostics-20261022/frontend.log)：25项通过。
- [真实在线日志](../data/processed/langchain-diagnostics-20261022/live-traces.json)：7项均结束且无协议错误，其中1项重排阶段超时、安全回退。

首次独立探针漏了脚本模块导入路径，在清理调用lease时失败；修正探针初始化后复跑，记录见`probe-first-invocation.json`。它不是网页运行路径的故障。

本轮解决的是可定位性，没有宣称降低上游超时率或保证每次回答质量。原文核验、硬约束和已有联合请求补全继续执行；尚未新增覆盖所有多意图问题的逐轮完整性检查。下一步应依据真实延迟样本讨论重排预算与回退后补核验的取舍，并把“无降级”和“回答有充分依据、是否完整”分别验收；不能仅用延长超时或一次复验成功作为质量保证。

复跑：

```sh
python -m unittest tests.agent.test_diagnostics tests.agent.test_services tests.test_service_runtime tests.test_qa_execution
COOKBUDDY_INTEGRATION_TESTS=1 python -m unittest discover -s tests
python scripts/verify_langchain_live.py --out data/processed/langchain-diagnostics-new
```
