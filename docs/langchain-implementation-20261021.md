# LangChain 在线编排实施与验收

日期：2026-10-21。依据：`AGENT_SPEC_LangChain结构.md` 与本项目的 `langchain_notes.md`。

## 本轮结果

后续生产化补齐：默认入口和 SSE 入口已启用跨任务 HTTP/Milvus 连接池、PostgreSQL 检查点与数据库会话锁；本页原700项验收作为迁移阶段记录保留，当前验证和部署步骤见[生产化补齐验收](langchain-production-20261021.md)。

LangChain 已从独立多 Agent 演示扩展到在线主流程，当前默认网页仍是 http://127.0.0.1:8080/ 。普通问答、库存选菜、原文/图片、连续追问、记忆接入、约束检查、降级与用量追踪现在共用新应用层。需要工具规划时执行 LangChain `create_agent`；明确事实、推荐和指代请求保留确定性快速路径，不强制增加规划调用。

另提供 http://127.0.0.1:8082/ 的 SSE 入口，实际支持 `/sessions`、`/chat`、带身份检查的 `/media`。已打开页面并验证库存推荐、第二个菜的原文及图片。

机器可读交付：[汇总](../data/processed/langchain-migration-20261021/summary.json)、[代码与配置快照](../data/processed/langchain-migration-20261021/manifest.json)。

检索、重排、切块、覆盖率和记忆版本/冲突核心没有改成 LangChain 实现。原多 Agent 实验入口保留；新主流程不会自动把复杂请求升级成多个 Agent。

## 验证证据

| 验证 | 实际结果 | 证据 |
|---|---|---|
| 后端完整回归，含真实 PG/Milvus 集成 | 700 项通过，无跳过 | [日志](../data/processed/langchain-migration-20261021/backend-scope-final.log) |
| 新编排专项测试 | 43 项通过 | `tests/agent/`，并包含在上述完整回归中 |
| 原网页前端 | 25 项通过 | [日志](../data/processed/langchain-migration-20261021/frontend-final.log) |
| 开发集检索适配前后 | 8/8 完成；两侧 Recall@5=1.0、MRR@5=0.9375 | [配对结果](../data/processed/langchain-migration-20261021/retrieval-pairs-v1/report.json) |
| 在线真实接口、工具、当前供应商 | 7 个场景的工程规则检查全部通过 | [记录](../data/processed/langchain-migration-20261021/live-scope-final/report.json) |
| 原网页任务队列兼容入口 | 两类联合请求均为`done`、`mode=langchain`，原文与图片工具齐全 | [默认入口结果](../data/processed/langchain-migration-20261021/default-entry-scope-final.json) |
| 稠密/稀疏与重排开关 | 两种模式各返回5条；稠密无BM25通道；关闭重排不调用重排阶段 | [真实切换记录](../data/processed/langchain-migration-20261021/retrieval-switches.json)及专项测试 |
| 依赖 | `pip check` 通过，锁定69个依赖，无版本升级 | `requirements-langchain.lock` |

在线7个场景：按番茄/鸡蛋库存推荐、第二个怎么做、再来一个、原文与图片联合查询、库外菜谱拒答、明确记住花生过敏、拦截含禁用食材的原文。这些是工程规则断言，不是独立人工质量评分。

检索对照使用已消费的开发集、相同配置和活动索引，顺序执行并可能命中缓存；不代表冷启动延迟、全库质量或新的封存测试。正式v3封存结果和旧消融失败均保留，未用本轮结果覆盖。

## L0–L6 落点

| 阶段 | 实现 |
|---|---|
| L0 | 核对安装版本/实际签名；假模型、async工具、隐藏上下文、checkpointer 与实际 hook 顺序跑通 |
| L1 | `src/llm/factory.py`、`src/agent/tools.py`、`build.py`；Agent和固定工作流共存 |
| L2 | 服务自校验、Guard返回前复查；身份不由模型填写；完整证据与模型消息分离；模板降级 |
| L3 | 复用共享限流/冷却/熔断/有限重试；独立阶段预算；模型/工具次数上限；子进程组超时取消和lease回收 |
| L4 | 既有画像/确认/版本链接入；确定性槽位和连续选菜；单一结构化摘要；after_agent非阻塞投递；M0不写任务记忆 |
| L5 | SSE事件、断连取消、真正的HTTP429准入；不可变配置、在线及受控实验文件、结构化Trace和实际HTTP用量 |
| L6 | 服务依赖边界、身份/线程隔离、日志隐私及middleware顺序测试；新增CI工作流与复跑入口 |

T-L1–T-L25 对应 `tests/agent/test_l0.py`、`test_online.py`、`test_services.py`、`test_api.py`、`test_config.py`。其中网络流式测试实际启动本地HTTP服务，取消测试实际回收进程；供应商故障与主备切换使用可编排假模型。CI工作流已写入，但未在远端GitHub执行。

## 关键行为

- `user_id/session_id` 从签名cookie和已验证会话产生。数据库RLS校验归属，thread_id用两者的组合哈希；同会话使用进程内锁和本机跨进程文件锁。
- 工具结果只给模型状态、最多8项来源和每项不超过200字的摘要。完整证据留在服务侧，详情展示上限4000字，超出提示截断；不向checkpoint灌入全文。
- 工具参数校验失败只返回固定错误，不回显完整参数；硬限制在来源返回前检查，违规结果不进入模型。显式记忆工具额外检查用户本轮确实授权，工具文本不能授权写入画像。
- 确定性直达与固定工作流共用相同服务、约束和Trace；不将它们虚报为已执行Agent middleware。
- after_agent仅执行有界队列 `put_nowait`，后台投递既有PostgreSQL任务；只提取用户消息。记忆失败不破坏回答，显式记住/忘记仍执行短事务及原有确认规则。
- Trace区分Agent逻辑调用、实际HTTP尝试和服务内部生成/改写/摘要调用，保留token和重排计费单位。未知用量/被取消请求不被解释为零成本。后台提取费用不混入本次前台回答的统计。
- 配置关闭外部追踪，并在执行上下文显式关闭LangSmith，避免环境变量意外启用原文上传。

实际middleware顺序为 Trace → Resilience → Memory → ContextSummary → Guard。before类hook按注册顺序执行，after类反向；wrap按顺序进入、反向退出，已用实测测试固定。摘要仅在循环开始时通过独立温度0结构化调用执行，不混入Agent工具决策循环。

## 与任务书骨架的适配及边界

1. 保留当前Python 3.12环境及LangChain 1.4.4、LangGraph 1.2.14，不为骨架改成Python 3.11或升级依赖。
2. 保留现有网页、持久队列和业务API，通过桥接接入；FastAPI SSE单独提供入口。旧队列仍有180秒外层预算，新LangChain分支另受默认30秒预算约束。
3. 迁移阶段使用异步SQLite及本机会话锁；后续已将默认配置切到PostgreSQL检查点、RLS隔离和数据库会话锁，SQLite保留为显式开发选项。
4. 旧同步服务仍通过可终止进程隔离，取消会终止子进程组并回收调用lease；后续已通过应用持有的私有Unix socket网络服务实现跨请求HTTP/Milvus REST连接复用。没有重写底层检索算法，也没有改成Milvus gRPC驱动。
5. SSE流出进度和经过核验的文本块；不会直接外发尚未通过依据检查的模型token。首段正文需等待核验，因此不能把它宣传为模型原生即时token流。
6. 使用自定义摘要middleware复用现有结构化输出和槽位权威存储；不另开一套内置摘要历史。记忆命名映射：off→M0、window→M1、slots→M2、profile→M3、profile_conflict→M5、full→M6；历史M4只以明确实验标签保留，线上不关闭必要确认和代码硬过滤。
7. 在线开关覆盖检索模式、重排、Agent与M0–M6；原E0–E8/A-1–A-6受控矩阵改为读取配置文件，已测试与旧协议逐字段一致。旧核心矩阵与生产矩阵仍分开，不冒充同一套在线等价消融。

本页记录本机在线编排迁移及后续补齐入口，不等于整个PRD已100%通过。连接池与共享PostgreSQL检查点的验证见后续报告；长期负载SLA、原PRD真人校准、账号配额与成本可接受性尚未认证。

## 复跑与回退

```sh
# 当前conda环境；新环境使用完整锁定依赖安装。
python -m pip install -r requirements-langchain.lock
python -m pip check
# 首次部署或更新检查点包时执行；应用运行不使用管理员权限。
python scripts/setup_langchain_postgres.py
make test-agent
COOKBUDDY_INTEGRATION_TESTS=1 make test
node --test tests/test_answer_rendering.cjs

# 原页面，默认使用新编排。
python scripts/serve_recipe_app.py --port 8080
# SSE入口。
make serve-langchain
# 独立工程冒烟，输出目录必须不存在。
python scripts/verify_langchain_live.py --out data/processed/langchain-live-new
# 开发检索配对。
make eval-langchain
# 示例：关闭rerank；其他配置见config/experiments/online/。
python scripts/serve_langchain_app.py --port 8082 --experiment config/experiments/online/P-A1.yaml

# 回退原编排，保留数据与索引。
python scripts/serve_recipe_app.py --port 8080 --legacy
```

基础数据库/向量服务仍按README的既有compose流程启动。备用模型仅在环境变量明确配置时启用，不擅自选择新供应商。

首轮真实联调的库存路由和“换一个”遗漏、阶段测试的旧文本断言失败，以及后续发现的联合请求只执行图片工具问题均保留，修复后另存复验。联合请求现在在预算内确定性补齐遗漏工具，并锁定用户指定菜谱；这不是把模型漏选记成成功。浏览器还发现旧软口味被当作库存硬条件的问题，已限制为本轮明确口味才参与库存硬过滤；硬限制不放宽。
