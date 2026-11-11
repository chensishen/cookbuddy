# 记忆系统改造与验收（2026-10-18）

按 `AGENT_SPEC_记忆系统.md` 完成 P1–P6 代码、迁移、工具/API 接入及本地回归。普通回答后的提取只读取用户消息、异步入库；画像更新保留版本链，解除硬约束必须确认；硬约束在检索前和返回前执行。**按用户要求切换到硅基流动 `deepseek-ai/DeepSeek-V4.1-Flash`，复用本地 Embedding 密钥。指定模型在线提取、摘要与改写已实际通过，原 HTTP 402 阻塞已解除。**

## 改动清单

- 短期：六轮窗口、滚动摘要、结构化槽位、指代解析、独立查询、24 小时会话过期；工具长文不写入短期消息历史。回答失败后仍保留任务条件，换菜与新增条件可继续执行。
- 长期：`scripts/cookbuddy/ltm/` 提供数据模型、用户消息提取、纯函数冲突决策、版本化画像、确认、事件向量存取、召回加权、维护和管理接口。会话、临时、全局偏好分开；库存默认三天有效；隐式偏好达到证据及会话门槛才采用。
- 过滤：`scripts/recipe_tools.py`、`scripts/search_test_recipes.py`、`scripts/preference_recommendation.py` 接入硬约束源过滤与返回检查。过滤导致不足时再次召回，不放宽限制；不安全的文本、图片、来源包整体拦截。
- 队列：`scripts/cookbuddy/worker.py` 只消费持久化用户消息。版本/清空 epoch 阻止迟到任务覆盖新偏好或恢复已清空记忆；向量投影和清空失败使用持久化重试。
- 接口与工具：认证上下文注入身份；四个记忆工具共用冲突决策和存储。支持查询、版本化修改/恢复、软删除、硬约束删除确认、导出、关闭和物理清空。
- 数据库：迁移 006–009 已在本地 PostgreSQL 应用；初始化脚本 `scripts/setup_memory.py` 幂等创建事件向量集合，并只迁移能够核对用户消息证据的旧画像。
- 维护：过期、衰减、容量淘汰、去重压缩、向量对账、会话结束及空闲提取；硬偏好不随软偏好衰减或容量淘汰。
- 前端：待确认事项位于答案卡片外；新建会话通知后台结束旧会话，不新增画像管理面板。

## 验收映射

下列自动化用例均纳入最后一轮全量测试；真实供应商模型成功不在这些通过结论内。

| 任务书用例 | 对应测试文件与检查 |
|---|---|
| T-S1–S3 | `tests/test_session_memory.py`：按展示顺序选择第二道、排除已推荐菜、修改临时条件并重新检索 |
| T-S4–S5 | 同上：摘要/窗口预算、失败保留槽位、工具原文不进入消息历史 |
| T-H1–H4 | `tests/memory/test_profile_guard.py`：花生、别名、空结果不放宽、显式冲突拦截；`test_live_integration.py` 补充真实画像和跨会话检查 |
| T-R1–R6、R8–R9 | `tests/memory/test_resolver.py`：作用域、版本链、撤销、确认、重复证据、临时过期、多值共存 |
| T-R7 | 同上：三次证据/两个会话门槛，隐式偏好不得静默覆盖显式偏好 |
| T-R10–R12 | 同上：并发单一 active、幂等投递、只提取用户消息；补充迟到任务、确认 epoch 与结构化输出重试 |
| T-E1–E4 | `tests/memory/test_events_api_maintenance.py`：反馈重要性、相关召回、低相似度排除、重复合并；`test_live_integration.py` 实测 Milvus 投影/召回/用户隔离 |
| T-A1–A2 | 上述文件与 `tests/memory/test_memory_http.py`：用户隔离、忽略工具身份参数、真实签名 Cookie HTTP 请求 |
| T-A3–A5 | 清空关系库/向量/缓存及防恢复、关闭后停止记忆读写、审计不含偏好原文；HTTP 验证确认/版本/导出 |
| T-M1–M2 | `test_events_api_maintenance.py`：衰减归档、事件容量淘汰且保留硬偏好、压缩保留反馈并幂等 |
| T-M3 | `eval/memory/evaluate.py --all`：逐题决策、配置/代码/数据哈希、指标与消融报告 |

**全量结果：465 项 Python 测试通过（包含真实 PostgreSQL、Milvus、HTTP 集成），22 项前端 Node 测试通过。**

## 实测报告

- [记忆诊断报告](../eval/memory/reports/20261018T143637160951Z/report.md)：15 个自建诊断对话、24 轮决策；M6 更新与最终画像正确率均为 1.0，指代正确率 1.0，小型提取诊断集 P/R 均 1.0，三道实际菜谱探测的硬约束违反率为 0。数字只适用于该样本集，不代表通用自然语言能力。
- [检索回归原始结果](../data/processed/cookbuddy-acceptance/memory-v2-retrieval-20261018.json)：八道现有 `prd-dev-v1` 有证据题，真实 Embedding/Milvus/Rerank；Recall@5 前后均 1.0，MRR 前后均 0.90625。真实硬过滤检索返回 39 个候选，违规 0。运行期间代码哈希未变化。
- [硅基流动在线验收结果](../data/processed/cookbuddy-acceptance/memory-v2-live-20261018-siliconflow.json)：六项检查全部通过，包含 LLM 提取、两次滚动摘要、指代改写、硬约束推荐、Embedding/Milvus 召回和清空。供应商返回四次记忆模型调用合计 1070 token，reasoning_tokens 均为 0；不包含其他请求费用。
- [旧供应商阻塞记录](../data/processed/cookbuddy-acceptance/memory-v2-live-20261018.json) 保留 HTTP 402 历史结果，未覆盖为成功记录。

评测脚本输出的本地 P95 增量不含网络/模型耗时；token 为保守字节上界，不是供应商计费。原任务书的 `eval_dataset_v1` 在仓库未找到，检索回归使用现有八道开发题，原流程由移除新增过滤插入点重建，不是历史部署快照或独立盲测。

## 与任务书的适配及边界

1. 沿用现有 `scripts/cookbuddy/` 目录、主 Pipeline、标准库 HTTP 服务与 PostgreSQL checkpoint；没有另建 FastAPI、Redis 或 `src/` 应用。已有多 Agent 分支继续使用 LangChain/LangGraph。旧记忆类仅保留兼容测试/历史工具用途，运行时走新模块。
2. 菜谱集合没有统一的结构化原料数组，因此根据完整原文生成允许的 source 集合，将表达式加入所有召回通道，并进行返回复核。来源无法确认不含禁用品时采取保守过滤；缺失难度等属性不编造。
3. PostgreSQL 按用户事务锁串行化冲突更新，API 显式校验版本；SQLite 使用事务锁，达到单一 active 和完整版本链的并发要求，未采用任务书示例的三次乐观重试。
4. 普通消息提取异步；用户明确记住/忘掉及管理操作按任务书工具约定同步走同一 resolve/apply。当前消息中的硬限制立即参与本轮过滤，不等待后台写入。
5. 不安全的无确认、无作用域及仅 Prompt 过滤消融只在隔离评测执行。线上不会为了实验关闭硬约束保护。M3/M4 的朴素行为是评测基线，线上仍保留必要确认。
6. 提取采用规则加结构化 LLM 后备；有规则结果时不再向 LLM 补充同一消息。有限规则和严格验证有识别边界，本轮小样本在线模型检查已通过，不能据此推断所有自然语言表达均可识别。
7. 清空物理删除记忆数据并阻止旧任务恢复；聊天记录独立保留，空设置/epoch 用于保证失效语义。远程向量删除失败返回 pending_retry，后台继续处理，不假报完成。

本仓库当前文件尚未提交，评测 commit 为 null；本文件是本地可审阅的改动说明，未创建 PR。部署、配置和复跑命令见 [记忆系统说明](memory.md)。

## 模型切换复核

聊天与记忆运行时使用相同 LLM 配置，密钥只存本地 `.env`，示例文件不写入密钥。硅基流动请求默认 `enable_thinking=false`，保留 JSON 输出与工具定义；已有显式重试参数仍可使用。参数依据[官方接口文档](https://docs.siliconflow.cn/docs/api/chat-completions-post)，并由真实调用及配置单测验证。全量 465 项后端与 22 项前端测试重新通过。

真实单 Agent 工具选择也已验证：对于“宫保鸡丁需要哪些材料？”，指定模型返回 `query_recipes` 工具调用，见[工具调用记录](../data/processed/cookbuddy-acceptance/siliconflow-tool-call-20261018.json)。该检查验证模型工具协议，未将一次工具选择当作完整菜谱回答验收。
