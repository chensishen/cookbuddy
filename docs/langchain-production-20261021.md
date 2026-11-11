# LangChain 连接池与 PostgreSQL 检查点补齐

后续更新（2026-10-22）：已补齐降级分类与阶段耗时，并再次捕获真实重排超时，见[诊断改进与实测](langchain-diagnostics-20261022.md)。本页707项回归及汇总仍为生产化补齐阶段的历史记录。

本轮完成前一阶段留下的两项工程工作。默认网页为 http://127.0.0.1:8080/ ，SSE 为 http://127.0.0.1:8082/ 。[机器可读验收](../data/processed/langchain-production-20261021/summary.json)与[文件快照](../data/processed/langchain-production-20261021/manifest.json)单独保存，未覆盖迁移阶段及原PRD封存结果。

## 实现与原因

原网页每个任务启动独立进程，单纯创建进程内 HTTP 客户端无法跨任务复用连接。现在每个应用启动一个持久网络服务，通过权限0600的本机 Unix socket 接受工作进程调用，持有16条有界 HTTPX 上游连接，空闲连接保留60秒。模型、Embedding、rerank、配方Milvus REST和记忆Milvus REST复用该服务。上游只允许配置的供应商与Milvus地址，不跟随跳转；请求独立传入认证头，不携带共享Cookie。

共享限流、重试、Retry-After、计费记录仍在原调用治理层执行，网络服务不自行重试。工作进程被取消或杀死后，本地连接断开会取消上游协程并关闭在途连接；空闲健康连接保留给下一个任务。应用关闭时销毁网络服务。非应用入口未配置 socket 时继续使用原 urllib 路径。

默认 `config/agent.yaml` 采用PostgreSQL检查点。使用官方 `langgraph-checkpoint-postgres==3.2.0` 与 `psycopg-pool==3.3.3`，保留原有依赖版本。独立 `cookbuddy_checkpoint` schema 由部署命令初始化，服务账号只有schema使用及三张检查点表的读写权限。表启用并强制RLS，每次事务设置隐藏上下文中的thread ID，归还连接时身份自动清除。thread ID仍由已验证的user/session生成，模型无法填写。

同会话串行锁改用PostgreSQL advisory lock，进程退出/取消会释放。检查点不自动迁移旧SQLite；业务对话和记忆仍由原PostgreSQL权威存储提供，每轮重建窗口和约束。PostgreSQL故障不会静默降级到本机SQLite。

## 验证范围

- [后端完整回归](../data/processed/langchain-production-20261021/backend-final.log)：含真实PostgreSQL/Milvus集成；计数见汇总。
- [7项专项](../data/processed/langchain-production-20261021/production-private-socket.log)：真实启动socket权限0600及关闭回收；不同客户端、先后退出的独立工作进程都复用同一条上游TCP连接；断连回收；429/Retry-After透传；地址白名单；独立进程恢复检查点；外部thread不可读、无身份直接SQL不可读；跨进程锁竞争有界、取消后可重新获取。
- [真实SSE联调](../data/processed/langchain-production-20261021/live-final/report.json)：首轮7/7；最终轮6/7达到无降级条件，库外问题在重排阶段安全回退，仍明确资料不足、无来源伪造。覆盖库存推荐、第二个、换一个、原文图片、库外拒答、记忆和禁用食材拦截。[回退诊断](../data/processed/langchain-production-20261021/degradation-trace.json)保留阶段记录；旧适配器合并记录超时和阶段错误，无法仅凭本次日志确定具体异常。2秒重排预算未为通过验收而放宽。[该场景独立复验](../data/processed/langchain-production-20261021/unknown-followup.json)单独记录，不覆盖末轮失败。
- [默认网页兼容入口](../data/processed/langchain-production-20261021/default-entry.json)：蒸水蛋与基础牛奶面包的原文/图片联合请求2/2，均执行两个工具并完成。
- [原前端](../data/processed/langchain-production-20261021/frontend.log)25项通过；[依赖检查](../data/processed/langchain-production-20261021/dependency-check.log)通过。

“共享PostgreSQL”已用独立进程验证，并未在两台物理机器上做故障/网络分区或负载验收。原网页任务队列、模型限流与产物目录仍为单机资源，不能据此宣称整个系统已经支持多机部署。持续负载SLA、原PRD真人盲评校准及账号配额/成本可接受性仍待验收。本轮未重新执行封存答案质量评测。

部署核对曾发现Uvicorn默认将socket改成0666，现由应用预绑定0600 socket再交给服务器；专项测试和[运行环境核对](../data/processed/langchain-production-20261021/infrastructure.json)均检查实际权限。[重启后冒烟](../data/processed/langchain-production-20261021/restart-smoke.json)另存。

## 部署和回退

```sh
python -m pip install -r requirements-langchain.lock
python scripts/setup_langchain_postgres.py
python scripts/serve_recipe_app.py --port 8080
# 或单独使用SSE入口
python scripts/serve_langchain_app.py --port 8082
COOKBUDDY_INTEGRATION_TESTS=1 python -m unittest tests.agent.test_production -v
```

初始化使用已有管理员数据库配置，运行时使用原应用数据库账号。每个节点只需有自己的网络服务，并连接相同PostgreSQL；网络服务只绑定Unix socket，不开放TCP端口。开发时可用实验YAML显式覆盖 `checkpoint: {backend: sqlite}`；回退原在线编排仍用 `--legacy`。回退不会删除PostgreSQL检查点或业务数据。

官方接口依据：[AsyncPostgresSaver](https://reference.langchain.com/python/langgraph.checkpoint.postgres/aio/AsyncPostgresSaver)。事务内身份与RLS由项目适配器添加，已针对锁定版本测试。
