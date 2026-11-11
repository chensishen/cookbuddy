# cookbuddy

2026-10-27 已改为单次意图理解、统一状态迁移和持久化上下文，移除旧在线编排与 `--legacy` 入口。模型恢复后，又修复组合请求、序号格式、定向替换、记忆通知与章节绑定冲突；最新后端默认回归816项通过、42项跳过，真实数据库与鉴权专项40项通过。真实Chrome复验口语推荐、缺料、换菜、错字章节图片及历史恢复追问，9轮历史记录均显示耗时；修复前失败也保留，不能据此承诺任意表达全面稳定。当前结果见[模型恢复后的复验](docs/live-dialogue-recovery-20261027.md)，架构与恢复前证据见[改造验收](docs/dialogue-refactor-20261027.md)。以下日期记录属于历史验收。

2026-10-22 第二轮浏览器测试的8项问题已修复并在真实Chrome复验：整餐模板、临时忌口、替代题降级与重复、连续换菜、章节图片、依据链接、SSE排版及个性化理由。740项后端（含真实数据库、无跳过）与29项前端回归通过。单条旧错误偏好已带审计归档，历史失败证据保留。见[第二轮修复验收](docs/browser-followup-repair-20261022.md)。

2026-10-22 修复本机模型生成被HTTPS重复校验拦截、记录导出KeyError、缺料条件误解析、记忆读取、整餐澄清、原文截断及同源卡片重复。725项后端、27项前端、最终7项真实SSE场景及Chrome中7个原失败场景通过。项目仍使用WorkBuddy本机模型代理；本轮范围、证据与剩余边界见[浏览器问题修复验收](docs/browser-repair-20261022.md)。

2026-10-22 已补齐重排降级原因与分阶段耗时诊断：真实复现确认一次回退停在HTTP调用阶段，2秒预算耗尽；没有放宽预算。712项后端与25项前端回归通过，在线7项均完成，其中1项安全降级。见[诊断改进与实测](docs/langchain-diagnostics-20261022.md)。

2026-10-21 补齐跨任务HTTP/Milvus连接池、PostgreSQL检查点、RLS隔离及数据库会话锁，默认8080和SSE 8082均已启用。707项后端、25项前端、7项生产化专项、2项默认网页联合查询通过；末轮7项真实在线场景中6项无降级、1项安全回退。首次部署新增 `python scripts/setup_langchain_postgres.py`；实现、证据与剩余验收范围见[生产化补齐验收](docs/langchain-production-20261021.md)。

2026-10-21 LangChain在线编排已接入默认网页，覆盖问答、库存推荐、连续追问、记忆、硬约束、降级与追踪；明确任务保留快速路径。700项后端、25项原前端回归通过，7项真实在线场景工程检查通过；8题开发检索对照Recall/MRR无下降。当前入口仍为 <http://127.0.0.1:8080/> ，SSE入口为 <http://127.0.0.1:8082/> 。`make test-agent`复跑专项，`make serve-langchain`启动SSE页。旧回退入口已于2026-10-27移除。实现、复跑和单机适配边界见[LangChain实施验收](docs/langchain-implementation-20261021.md)。下面的PRD封存指标属于迁移前版本，不自动认证新编排的全量回答质量。

2026-10-21 本轮自动化收尾：原配方用量直答、来源隔离封存v3与扩展消融已完成。新封存检索Hit@5为95/96，生产Agent独立judge12/12；Ragas 32条/144项齐全。新增96条核心复验和48条生产留一法，全部生成及评分完成，旧失败保留。657项后端与25项前端回归通过。严格门槛现为**12通过、0未达标、2待验收，整体部分通过**；剩余真人校准与成本可接受性，扩展格式、其余参数和生产SLA仍按已验证范围说明。见[当前交付](docs/prd-final-delivery-20261021.md)、[新封存结果](docs/heldout-v3-results-20261021.md)和[扩展消融](docs/expanded-ablation-results-20261021.md)。一条命令复跑两层矩阵：`make eval-expanded`。

2026-10-20 真实规划诊断：固定12题调用当前DeepSeek模型，首轮过滤参数12/12正确，完整问题保留11/12；强化共享提示后同题复测12/12通过，首轮失败保留。614项后端测试通过；这是经典Agent首轮开发诊断，完整PRD仍为部分通过。见[真实规划记录](docs/agent-filter-planning-20261020.md)。

2026-10-20 Agent过滤接入：经典Agent和LangChain工具支持分类、难度上限，保留完整问题，过滤条件贯穿本地直答、缓存、子进程及降级，并检查返回来源。608项后端测试通过；脚本化规划验证返回3道合规推荐，未测试真实模型抽取准确率。见[Agent过滤记录](docs/agent-search-filters-20261020.md)。

2026-10-20 通用检索过滤：命令行及代码入口支持显式分类、难度上限，四路召回统一执行来源白名单并与记忆硬限制取交集。593项后端测试通过，真实Milvus只读验证通过；Agent参数接入和过滤收益消融尚未完成。见[检索过滤记录](docs/search-filters-20261020.md)。

2026-10-20 供应商诊断：两条HTTP链路新增受限错误码和限流响应头记录，过滤消息、正文及任意敏感字段。584项后端测试全部通过；本地429故障注入验证记账与重试。见[诊断修复记录](docs/provider-diagnostics-20261020.md)。完整PRD仍为部分通过。

2026-10-20 调用治理：修复429的Retry-After仅约束当前调用的问题，新增跨进程共享冷却、预算检查与恢复验证。577项后端测试全部通过；本地HTTP及子进程故障注入确认冷却期间没有追加API请求。见[共享冷却修复记录](docs/shared-provider-cooldown-20261020.md)。

2026-10-20 继续改进：修复字面问题多余的记忆/规划调用、规划失败的原文降级、空回答评审误计分及顶层执行状态漏计。571项后端测试通过；两道真实Agent回归各一次规划，约1.31/1.21秒，5条引用元数据正确。完成独立切块/前缀对照；第二份封存测试检索Hit@5为100%，但429导致严格验收仍部分通过。证据见[继续改进记录](docs/prd-continuation-20261020.md)，两份封存结果与修复回归分别保留。

2026-10-20 后续改进与验收：新增同源512-token稠密基线、32份独立来源100题测试、独立Qwen评审、五项Ragas和50条真人盲评包。修复原文降级引用漏传菜名及明确原料/流程问法的慢调用；558项后端与25项前端测试通过。首次封存Hit@5为98.96%，原始未达标结果与修复回归分别保留。完整PRD仍为**部分通过**；详见[本轮收尾记录](docs/prd-closeout-20261020.md)。

2026-10-20 调用链改进已完成并复验：重排默认10候选、排队上限0.5秒，两项语义复核默认关闭思考，明确单目标直接路由；535项Python测试（含真实PostgreSQL/Milvus，无跳过）与25项前端测试通过。最终主集Agent P95为4.65秒，跨步骤生成题仍需约6–10秒。完整PRD仍为**部分通过**，质量、参数对照、分阶段证据和未达标项见[本轮改进验收](docs/latency-improvements-20261020.md)。

2026-10-19 切换硅基流动 `deepseek-ai/DeepSeek-V4.1-Flash` 后，[PRD 重测](docs/prd-retest-siliconflow-20261019.md)的 `make eval` 全流程退出码为 0，完整 Agent 5/5 正常返回、库外拒答 2/2，judge 与 Ragas 均完成。整体验收仍为部分通过：库存题评审分歧、重排超时及收益不显著、延迟、独立测试和成本等问题仍待处理。下文保留之前各轮记录。

## PRD 整体改进与验收（2026-10-19）

本轮修复两个 Milvus 的内嵌 etcd 启动竞态，改为各自独立 etcd 健康后启动，保留原卷和 `recipe_bounded_v3` 的372份来源、2074个块。现有 compose 命令会启动五个容器。新增 XLSX 候选解析，补齐 DOCX 标题/表格、公式说明绑定、异常文件隔离，限制重排为默认30个候选，并修复失败漏计、降级误计重排、旧供应商价格误用及快速熔断延迟误读。

512项回归（含真实数据库、无跳过）和25项前端测试通过，浏览器红烧肉做法与食材覆盖率推荐通过。结论仍为**部分通过**：真实检索开发集 Hit@5 为100%，重排增益不显著；该轮使用的 `agnes-3.0-flash` 调用出现 HTTP 429，完整回答、拒答、judge/Ragas、盲测和完整成本尚未全部通过。当前配置以本地 `.env` 为准，下文旧日期的模型说明属于历史记录。逐项证据、实际回归结果和复跑方式见[本轮验收](docs/prd-acceptance-20261019.md)。

当前入口：[cookbuddy](http://127.0.0.1:8080/)。简洁双栏聊天、PostgreSQL 历史会话、后端长短期记忆与 Hybrid Intent Router，前端无需选择意图或管理画像。提供六类提示词模板。当前记忆实现与验收见 [记忆系统](docs/memory.md)。

启动基础服务：`docker compose --env-file .env -f deployment/cookbuddy/compose.yaml up -d`；初始化：`python scripts/cookbuddy_manage.py init`；应用：`python scripts/serve_recipe_app.py --port 8080`。

连续选菜使用分句条件解析和独立任务状态表，回答失败不会清空当前食材与排除条件。升级请执行上述初始化命令应用迁移 004；旧失败会话可按用户原话恢复。实现和真实四轮页面验收见 [选菜上下文重构](docs/task-context-refactor-20261017.md)。

2026-10-18 记忆系统已按新任务书改为六轮窗口、滚动摘要、槽位、版本化画像、硬约束双重过滤、确认与事件召回。后台仅从用户消息提取；旧的菜谱知识/助手总结不再进入运行时记忆。升级请执行 `python scripts/setup_memory.py`，会应用迁移并初始化事件向量集合。`python scripts/cookbuddy_manage.py health` 仍可查看队列状态。旧 [记忆提取验收](docs/memory-extraction-20261016.md) 保留作历史记录；当前状态见 [本轮验收](docs/memory-implementation-20261018.md)。  
基于HowToCook与Milvus的本地学习项目。当前已经能在页面中查询菜谱、比较食材、查看原文和步骤图片，并记录工具调用及来源。当前版本是可运行的学习MVP，尚未宣称全库推荐准确性或多模态理解全面通过。

## PRD 整体改进与验收（2026-10-14）

当前聊天生成、来源复核、Agent 及记忆提取/摘要/改写共用 **`workbuddy-ai/deepseek-v4.1-flash`**，通过本机 OpenAI 兼容代理 `http://127.0.0.1:3425/v1` 调用。本地 `.env` 使用项目配置项 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`；密钥不写入文档。启动前需运行本机代理。Embedding、重排模型和向量空间保持原配置；下文旧模型验收为历史记录。

本轮增加全库原料表覆盖率推荐、食材别名归一化与工具过滤，修复简单库存问题的原料表召回范围；新增统一候选 IR、HTML/CSV/TSV、表格重复表头、代码/公式原子分块和质量隔离。候选解析不会自动发布到活动 Milvus 集合。

可复跑入口：

```sh
conda activate py3.12.11
python -m venv .venv-ragas
.venv-ragas/bin/python -m pip install -r requirements-evaluation.lock
make test
make eval             # 真实供应商请求：基线、消融、自建judge、Ragas忠实度
make eval-offline     # 来源检查与BM25；不调用远程模型
```

结论为**部分通过**：功能链路与全库溯源通过，开发诊断题集不等于独立盲测；重排显著性、98%语义引用准确率、完整成本预算及其余Ragas指标仍需验收。逐项证据、历史首轮失败和复跑说明见 [本轮整体验收](docs/prd-acceptance-20261014.md)。以下数量与旧模型说明属于历次阶段记录。

## 工程补齐与测试（2026-10-13）

新版本已加入长章节子块、中文 BM25 与向量的 RRF 融合、版本隔离发布和回滚。服务使用 SQLite 持久任务队列、两个执行进程槽、有界排队、幂等提交、取消和整次查询180秒预算；模型调用具有跨进程限流、有限重试、熔断和阶段诊断。多 Agent 增加 SQLite checkpoint 与原文／图片的独立并行工具。PDF、DOCX 和扫描件 OCR 解析保留为带来源定位的候选，未经复核的 OCR 不进入配方结论。

完整的实现与测试对应表见[工程补齐验收](docs/engineering-implementation-and-tests.md)，面试解释见[设计逻辑与追问](docs/rag-agent-interview-guide.md)。验收包含单元与故障注入、真实检索重排对照、Agent对照、页面负载与重启测试；这些结果不代表生产集群容量或全库准确率。

按文档解析、向量召回、重排、Agent、LangChain、并发和超时逐项核对的进度见[项目完成度与面试需求核对](docs/project-completeness-review.md)。面试文档第16、17章提供真实请求追踪、深入追问和证据链接。

后续批次见[八题问答质量验收](docs/quality-batch-v1.md)与[问答耗时优化](docs/qa-latency-v1.md)。耗时优化批完成时246项测试通过；默认问答在完整指定来源时省去前置判断、并行独立复核，仍保留原文校验与故障降级。

最新已加入[菜谱目录与来源选择](docs/recipe-catalog-v1.md)，项目258项测试通过。展开页面“浏览菜谱目录”可搜索菜名／章节、按分类分页并直接打开原文或图片；同名菜谱可点击选择具体来源，不用手写路径。目录按372份来源、371个菜名分别计数。

运行交付批完成时274项测试通过。后续阶段验收新增全库来源覆盖、同机独立依赖环境复现与小规模在线并发，当前**278项测试通过**，见[阶段验收与进度](docs/stage-acceptance-v1.md)。统一入口与故障提示见[运行、检查与演示](docs/project-run-delivery.md)：

```sh
conda activate py3.12.11
python scripts/project.py check --live
python scripts/project.py start --port 8081
# 另一个终端执行不调用远程模型的演示。
python scripts/project.py demo --port 8081
```

启动入口默认本地模式，界面、API和执行进程均禁止模型任务。完整模式使用`python scripts/project.py start --full`；密钥权限与余额不会由检查入口自动验证。

## 打开应用

应用地址：<http://127.0.0.1:8080>

在本项目目录启动：

```sh
python scripts/serve_recipe_app.py
```

当前版本统一使用 conda `py3.12.11` 环境，先执行 `python -m pip install -r requirements-langchain.lock`。端口占用时可加`--port 8081`。服务仅绑定本机，不公开到外网；关闭时在启动终端按Ctrl+C。

问答需要Milvus容器，以及本地`.env`里的本机代理聊天配置和原有Embedding/Rerank供应商配置。当前使用 compose 管理的 `cookbuddy-recipe-milvus-1`、`cookbuddy-memory-milvus-1` 和 `cookbuddy-postgres-1`，按文首命令启动；旧 `milvus-standalone` 不再使用。当前 Milvus 集合为`recipe_bounded_v3`，372篇菜谱、2074个章节／子块。原文和图片工具读取本地文件，不依赖模型API。

- “智能选择工具”：DeepSeek-V4.1-Flash 根据问题选择工具，展示工具返回及执行过程。
- “多 Agent 协作（LangChain）”：协调者分派给菜谱问答或原文／图片专员，继续展示经原有工具检查的结果。
- “菜谱问答与食材推荐”：直接走文本Embedding → Milvus召回与补召回 → Qwen3-Reranker-8B → 来源校验后的回答。
- “查看菜谱原文”：按完整菜名与可选章节关键词展示逐字原文。
- “查看菜谱图片”：查询原图、作者图注及其章节位置。

建议先点击页面示例“看蒸水蛋原文”或“看酵头图片”，再测试“核对凉拌黄瓜用料”。使用智能模式时也可以输入“请给我看基础牛奶面包酵头的图片”。

## 命令行入口

```sh
python scripts/recipe_agent.py '请给我看基础牛奶面包酵头的图片'
python scripts/recipe_agent.py '空气炸锅面包片应该用多少度、烤多久？' --mode rag
python scripts/recipe_agent.py '查看蒸水蛋原文' --mode recipe --title 蒸水蛋 --section 操作
python -m unittest discover -s tests
```

原有单 Agent 只可调用三个白名单工具：`query_recipes`、`read_recipe`、`find_recipe_images`。最多三轮模型决策、四次实际工具调用，同一请求不会重复执行相同参数的工具。模型决策与直接工具模式明确区分；API失败不伪装成模型成功，已完成工具结果可保留。

## LangChain 多 Agent 模式

在用户指定的 conda 环境中安装依赖并启动页面：

```sh
conda activate py3.12.11
python -m pip install -r requirements-engineering.txt
python scripts/serve_recipe_app.py
```

页面选择“多 Agent 协作（LangChain）”，也可执行 `python scripts/recipe_agent.py '请同时查看基础牛奶面包酵头的原文和图片' --mode multi_agent`。协调者与两名专员只负责分派和工具选择，最终回答仍取自已有的来源检查工具。多 Agent 会增加模型调用；目前保留为与单 Agent 对照的可选模式。架构、职责边界和真实测试见[LangChain 多 Agent 模式](docs/langchain-multi-agent-v1.md)。

## 数据与回答

回答按“简短回答＋原文依据＋来源”展示。Agent负责选择工具；最后展示工具经过已有检查的回答，不再让规划模型任意改写缺料、可选项或温度。来源检查与模型语义复核能降低错误，仍不等于全库事实已被人工认证。

图片索引覆盖354张本地图片，关联菜谱章节和来源行。三张OCR与视觉描述试验单独保存，未启用为配方事实。图片工具使用作者原图与原文图注，不能只根据照片确定内部熟度或配方数值。没有处理未出现的PDF、Word或音视频。

三篇人工结构化样本已接入有限范围的基础库存比较：宫保鸡丁、凉拌黄瓜、基础牛奶面包。其他推荐沿用原料表比较与原文章节核对，不保证数量、工具、过敏忌口和复杂替代条件完整。五篇自动抽取试验仍有未解决项，没有自动推广到全库。

另有五篇菜谱的有限来源条件规则，用于明确库存、缺料、可选项与特定版本用料问题。8道相关题和2道新增问法已做实际RAG检查，项目103项测试通过；本分支按程序规则回答，不把它称为模型生成提升。具体适用范围见[条件判断修复报告](docs/ingredient-condition-improvement.md)。

后续增加了明确菜名的同菜谱上下文、拒答前尝试、独立完整性检查，以及四类有限来源回答。第4、7、10、11题实际复测通过，当前119项项目测试通过。通用模型回答仍不稳定，品牌负例仍有校验失败，详见[回答完整性修复报告](docs/answer-completeness-improvement.md)。

最新增加两篇菜谱的来源公式计算，保留固定量、区间、向上取整和依赖，白糖缺少缩放公式时不猜数值。两道原题、四道新增问法实际检查通过；当前134项测试通过。范围见[数量计算修复报告](docs/recipe-quantity-improvement.md)。

Agent现增加用户请求范围检查，避免擅自加章节筛选，并分开记录模型提议和实际执行。两道原题与两道明确筛选查询通过真实工具调用复测，当前144项测试通过，详见[Agent范围修复报告](docs/agent-scope-improvement.md)。

完整30题复测已完成：首轮22题通过、5题部分满足、3题执行失败；三题各补测一次后的选定结果为24题通过、5题部分满足、1题未通过。该轮第30题的缺失属性结论被校验拦截，后续已完成针对性修复。详情和首轮／补测记录见[整体复测报告](docs/full-regression-v2.md)，这不是全库准确率。

## 记录与项目说明

- `data/processed/agent-runs/`：每次工具执行、状态、模型决策返回元数据、回答及来源记录。
- `data/processed/all_recipes/`：分块、向量缓存、检索、重排和回答记录。
- `data/processed/media_pilot/`：图片来源索引及三张解析样本。
- `data/structured/`：人工样本、自动抽取试验及检查记录。
- [本轮功能与验收](docs/agent-mvp.md)
- [第一批30道评估参考题（待核对）](docs/recipe-evaluation-v1.md)
- [第一轮30题真实测试结果与修复优先级](docs/recipe-evaluation-baseline-v1.md)
- [必需、可选与否定条件修复及复测](docs/ingredient-condition-improvement.md)


- [已有依据误拒答与章节比较修复](docs/answer-completeness-improvement.md)
- [数量公式计算及复测](docs/recipe-quantity-improvement.md)
- [Agent章节筛选与工具选择修复](docs/agent-scope-improvement.md)
- [完整30题复测与剩余问题](docs/full-regression-v2.md)
- [图片处理方式](docs/recipe-media-processing.md)
- [结构化抽取结果与限制](docs/structured-extraction-completion.md)
- [全量知识库导入说明](docs/all-recipes.md)
- [最初方案草案](docs/rag-plan.md)

本地`.env`中的密钥只由后端读取，不出现在网页或执行记录中。菜谱版权与许可证沿用`data/HowToCook`仓库原文件；新增记录不替代原作者内容。

当前阶段已收尾：缺失属性通过完整原文单独核对，三项真实对照完成，152项测试通过。下一阶段转入新增菜谱使用验收，5个旧题部分满足项保留为待办。见[阶段收尾与使用验收](docs/stage-closeout-and-usage.md)。

新增菜谱的第一批六场景验收已记录：3项符合预期、1项部分满足、1项校验失败、1项接口超时阻断。见[使用验收结果](docs/usage-acceptance-v1.md)。

针对第一批验收的三类问题，新增受限的原文步骤摘录、调料分类与明确库存核对；三题真实RAG复测通过，158项测试通过。方法与未覆盖范围见[成批改善记录](docs/usage-batch-improvement-v1.md)。

下一批多模态试验对6张图片完成本地OCR与来源核对，提供只读的图片文字候选检索；包装字样、刻度与手写配方数字均未作为菜谱事实启用。162项测试通过，见[图片OCR试验](docs/media-ocr-pilot-v1.md)。

经助手看图核对的3条图片文字现可用于图片关键词查找：例如在“查看菜谱图片”模式中输入菜名“戚风蛋糕”、关键词“玉米油”可找到手写配方图。图中文字会单独标为找图线索，不能当作菜谱用量；164项测试通过。见[图片文字核对与接入](docs/reviewed-image-text-v1.md)。

结构化抽取的5篇旧试验已做离线参考核对：1篇边界通过但语义待核、1篇检出6项遗漏或差异、3篇仍无有效草稿；全部保持禁用。166项测试通过，详见[离线复评](docs/structured-offline-audit-v1.md)。

可乐鸡翅与蒸水蛋现有独立的来源修订候选及4组隔离库存对照：两篇均通过结构、逐字引用和扩展边界检查，但未接入正式问答；170项测试通过。见[结构化修订试验](docs/structured-repair-pilot-v1.md)。

另三篇旧试验失败菜谱现已建立原文优先候选；五篇共74实体、176章节观察，完成7组隔离库存对照并保持正式问答禁用。174项测试通过，阶段边界见[五篇结构化候选收尾](docs/structured-five-candidate-closeout.md)。

正式问答已增加不含问题原文或密钥的模型调用阶段诊断。两条蒸水蛋真实链路复测分别验证了无依据原因被拒绝、有原文依据的盖碗方式通过；175项测试通过。记录及限制见[问答调用诊断与复测](docs/model-call-diagnostics-and-smoke-v1.md)。

本地应用的原文、本地图片、原文外链、直接问答和Agent选图五个场景已通过页面接口验收；浏览器确认外链提示及三张原图展示。原文外链现单独标记为“未下载、内容未核验”，不会充当配方依据。176项测试通过，复现方法见[本地应用五场景验收](docs/app-acceptance-v1.md)。


菜谱检索服务已于2026-10-15恢复，新增持久化 recipe-milvus 服务；故障分析、索引恢复与复测见 [问答服务恢复记录](docs/rag-recovery-20261015.md)。

2026-10-26：问答入口改为模型理解、程序校验，支持口语、错字和尾部噪声，并统一章节与序号处理。实现、兼容性和验收范围见 [理解层设计](docs/nlu_design.md) 与 [本轮验收](docs/nlu_acceptance_20261026.md)。

2026-10-28：统一餐次词表、按餐次确定性菜单、依赖隔离的滚动熔断与准确降级提示。配置和复现命令见 [实现说明](docs/degradation_design.md)，实际测试结果及未覆盖边界见 [验收记录](docs/degradation_acceptance_20261028.md)。
