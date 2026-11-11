# cookbuddy 改造与验收 · 2026-10-14

## 用户可见行为

页面采用简洁的双栏聊天布局：左侧历史会话、新对话；右侧消息、菜谱卡片和固定输入框。手机端以抽屉打开历史会话。项目名为 **cookbuddy**。前端不再提供长期记忆面板或意图/模式选择器，不使用 localStorage 保存长期记忆，也不向后端传入记忆、身份或工具模式选择。

历史会话和消息保存于 PostgreSQL；同一浏览器的签名会话 Cookie 恢复历史。消息逐字、安全渲染。处理期间可展开真实后端阶段及耗时；卡片的做法、原文和图片按钮发送普通自然语言问题，仍由后端路由。首页提供六类可点击模板：食材推荐、分步做法、一餐安排、细节核对、原文与图片、个人偏好。未替换的【占位内容】会提示用户补齐。

## Long-term Memory

- PostgreSQL 为权威存储：画像 `profile`、历史任务 `task`、来源知识 `knowledge`。结构包含置信度、重要性、证据、版本、有效期、最后访问时间与状态；独立迁移、索引和 RLS 租户策略。
- Memory Extractor：DeepSeek Flash 后台提取。画像需要用户本条明确陈述及逐字证据，不从助手回答推断用户画像；拒绝假设、问句、低置信度及白名单外画像字段。知识需要已核对工具证据，原文路径、逐字摘录和文件哈希。持久化内容采用实际证据，避免把模型润色当作事实。任务记录成功任务与来源，不把未完成或降级结果当知识。
- Milvus：独立记忆服务 `127.0.0.1:19532`，独立集合 `cookbuddy_memory_<embedding-space-hash>`；保存情景向量及用户/记忆ID/版本，不与菜谱集合混用。Embedding 配置变化会改变集合命名；提供 `reindex` 回填命令。
- Retriever + Rerank：用户过滤的向量召回，使用 PostgreSQL 再检查当前版本、状态和 TTL；来源知识检查原文件哈希。画像作为结构化候选，其他记忆与当前问题重排，限定上下文字数。检索或重排失败会记录降级状态；不会绕过 PostgreSQL 的遗忘与版本检查。
- Prompt 注入：推理前结合短期上下文和检索记忆整理独立问题，当前要求优先。工具 Agent 及多 Agent 系统 Prompt 还注入被标记为数据的记忆上下文；记忆不能替代本次菜谱证据。
- 更新：同一用户、类别和稳定字段更新同一记忆ID，版本递增、记录修订，事务内写入向量同步 Outbox。
- 压缩：任务数量超过40条时，对较旧任务按来源分组合并为保留原始记忆ID的摘要；原任务标记 compressed。采用确定性摘要，不允许压缩模型增添事实。
- 遗忘：画像默认365天、任务90天、知识180天；读取即排除过期记忆，后台定期清理并同步向量删除。操作命令支持精确ID或全部遗忘；聊天发送“请忘记我的所有长期记忆”立即失效并停止旧提取任务。记忆 epoch 防止在途旧提取任务恢复已遗忘内容。历史会话仍保留，记忆遗忘不等于删除聊天历史或数据库备份。
- 后台可靠性：持久任务、SKIP LOCKED 原子认领、180秒租约、指数退避、最多5次重试、失败状态与重新调度命令。Outbox 重放以当前 PostgreSQL 版本为准；向量同步与遗忘通过用户锁协调。

## Intent Recognition

- PostgreSQL 动态 Intent Registry 保存描述、示例、目标、版本和启停状态，每次请求加载当前启用版本。默认8类：菜谱事实、库存推荐、原文、图片、原文+图片、一餐安排、一般对话、澄清。
- DeepSeek 分类并提取菜名、章节、食材与限制；Embedding 匹配各版本意图原型，原型向量按模型空间及注册版本缓存。
- 混合分数为 `0.72 × LLM confidence + 0.28 × cosine`；阈值、候选分差及双路分歧共同决定接受或澄清。分数是启发式排序/门控分数，**尚未校准为概率**。Embedding 不可用记录降级，低置信度不强行调用工具。
- 路由到现有 RAG、工具、单 Agent、多 Agent 或澄清流程。工具菜名必须匹配当前真实知识库，章节不得由模型自行添加。
- 注册目标采用代码内允许列表；新增 Intent 通过运维 CLI，不开放未经授权的公共注册接口。真实测试已验证新增意图无需重启即可识别。
- 每次决策保存模型分类、混合分数、分差、实体、注册版本和降级信息，便于回放及后续置信度校准。

## 安全、持久化和运行

PostgreSQL 应用角色不是 superuser，也没有 BYPASSRLS；画像、消息、会话、修订、任务及 Outbox 强制租户隔离，会话与消息有复合租户外键。浏览器签名 Cookie 为 HttpOnly/SameSite=Strict，身份由服务器分配，客户端不能指定 user_id。新任务先处于 preparing，用户消息持久化成功后才进入执行队列。任务查询和取消检查所属用户，提交幂等且不会重复写消息。模型错误只保存异常类型，不输出凭据或完整异常连接串。

本地 Docker Compose 采用专用 PostgreSQL 和记忆 Milvus 数据卷、仅绑定 loopback、健康检查、服务自动重启。密钥、数据库密码和 Cookie 签名密钥仅在权限600的本地 `.env`。

启动：

```sh
docker compose --env-file .env -f deployment/cookbuddy/compose.yaml up -d
/opt/anaconda3/envs/py3.12.11/bin/python scripts/cookbuddy_manage.py init
/opt/anaconda3/envs/py3.12.11/bin/python scripts/serve_recipe_app.py --port 8080
```

后台进程可独立运行 `python -m cookbuddy.worker`（设置 `PYTHONPATH=scripts`）；本地服务默认包含后台线程。多实例发布应拆出独立 worker，并为 Web、迁移及 worker 分配独立的最小权限凭据，使用真实账号认证、TLS、连接池/容量规划、备份恢复和监控告警。这些生产部署项不以当前本地演示替代验收。

运维：

```sh
python scripts/cookbuddy_manage.py health
python scripts/cookbuddy_manage.py worker-once
python scripts/cookbuddy_manage.py retry-failed
python scripts/cookbuddy_manage.py reindex
python scripts/cookbuddy_manage.py maintain --user <用户ID>
python scripts/cookbuddy_manage.py forget --user <用户ID> [--memory-id <记忆ID>]
python scripts/cookbuddy_manage.py register-intent --file /absolute/path/intent.json
```

新增 Intent 文件示例：

```json
{"ident":"oven_query","description":"烤箱温度与模式问答","examples":["烤箱预热到多少度？","烤面包用什么模式？"],"target":"rag"}
```

实现参考：[PostgreSQL/psycopg 事务](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)、[Milvus 官方部署](https://milvus.io/docs/install_standalone-docker-compose.md)。本项目使用本地单机部署配置。

## 验收结果与生产门槛

317 项自动化测试通过（启用真实 PostgreSQL HTTP 集成测试）覆盖原有菜谱功能、提取证据、路由门控、向量版本过滤、任务准入、会话/任务权限、客户端身份注入拒绝及提交幂等。真实 PostgreSQL + Milvus + DeepSeek 集成结果保存于 `data/processed/cookbuddy-acceptance/integration-live.json`，所有列出的检查通过：跨租户读取为0、向量过滤、更新版本、TTL/压缩、旧任务不可恢复遗忘数据、物理向量删除、LLM 自动提取、Retriever + Rerank、Hybrid Router 和动态 Intent。

浏览器实测：原文自动路由、返回3张酵头原图、跨会话记忆召回、历史会话切换、模板填入、桌面双栏及手机布局。旧菜谱 Milvus `127.0.0.1:19530` 仍有 etcd 启动时 leader changed 导致异常退出；新记忆 Milvus健康运行。完整菜谱向量问答仍有原文候选降级限制，本轮不修改旧菜谱向量数据。

**当前结论：核心模块与本地集成验收通过，生产环境准入尚未完成。** 仍需独立标注意图测试集与置信度校准、自动画像长期冲突/召回质量评测、负载/故障恢复/备份恢复验收，以及修复原菜谱 Milvus 服务。当前签名浏览器会话是本地用户隔离，不能替代正式账号与跨设备认证。知识安全和引用正确率沿用此前 PRD 验收边界。


## 图文混排与记忆复测

图片按 source + source_line 插入对应原文位置，展示周围步骤，保留原图图注；原文工具附带已核验图片，图片工具附带所在章节原文。不凭照片生成配料、温度或熟度。2026-10-14 复测：317 项测试通过，真实数据库、向量库和 DeepSeek 联调17项全部通过，浏览器确认酵头文字和3张图片按原文顺序交错展示。旧会话记录不会重写，新请求采用新结构。

可在项目目录执行：

```sh
COOKBUDDY_INTEGRATION_TESTS=1 /opt/anaconda3/envs/py3.12.11/bin/python -m unittest discover -s tests
/opt/anaconda3/envs/py3.12.11/bin/python scripts/verify_cookbuddy.py --llm
```

手工检查应使用专门的测试浏览器会话：

1. 发送“请记住，我喜欢清淡口味，家里有空气炸锅。请查看蒸水蛋原文。”，等待后台提取任务完成。
2. 新建对话，问“按我的口味介绍蒸水蛋的做法”。检查对应任务返回值 memory_context.count/ids、context_question，并核对实际回答；仅看模型说“记住了”不算验收。
3. 在测试会话改口“请记住，我现在喜欢微辣。请查看蒸水蛋原文。”，等待提取完成，核对同一画像字段版本递增、内容更新。
4. 在测试会话发送“请忘记我的所有长期记忆”，新建对话后核对长期记忆不再注入。不要在原对话验证遗忘，因为短期历史仍存在。
5. 用无痕窗口作为另一个用户，核对第一用户的历史和记忆不可见。

memory_context 在后端任务结果中，用浏览器开发者工具查看 /api/job 返回 JSON；不在产品界面新增记忆面板。实时向量物理删除与自动提取异步完成，读取时 PostgreSQL 的状态、版本和 epoch 已先行保护。TTL、超过40条任务的压缩和在途旧任务遗忘通过集成脚本验证，无需人工等待90天。


2026-10-15 的记忆一致性修复与321项回归结果见 [记忆专项验收](memory-acceptance-20261015.md)。
