# 项目进度、完成度与面试需求核对

日期：2026-10-13。核对依据是当前代码、测试和保留的真实运行记录。目标是说明“已实现什么、为什么这样实现、如何展示证据”，不以文件数量估计完成百分比。

## 结论

用户要求的RAG知识库＋Agent工具调用链路以及所列面试问题，当前已具备实现、设计解释和对应测试。可以用于本地演示和项目面试。完整说明集中在[设计逻辑与面试追问](rag-agent-interview-guide.md)，新增第16章真实请求逐步追踪、第17章深入追问。

这是学习MVP的功能与工程完成度；全库泛化质量、生产容量、全新机器部署和跨主机高可用仍未完成验收。模型核验不是事实证明，来源命中也不是生成正确率。

## 按用户要求逐项核对

| 要求 | 实际实现与设计取舍 | 代码／测试／记录 | 状态 |
|---|---|---|---|
| 文档解析 | Markdown章节与行号；PDF、DOCX、扫描OCR为隔离候选，保留页码／节点／置信信息 | [解析器](../scripts/document_ingestion.py)、[解析与检索测试](../tests/test_ingestion_and_hybrid.py) | 正式Markdown已完成；其他格式候选已实现，未成为正式配方库 |
| 为什么这样切 | 按菜谱副标题保留原料、计算、操作；长章节按完整行再切，768为token代理，默认无重叠 | [菜谱分块](../scripts/chunk_test_recipes.py)、[版本准备](../scripts/prepare_recipe_version.py)、[解析测试](../tests/test_ingestion_and_hybrid.py) | 已实现和测试；不声称参数最优 |
| Embedding与向量库 | Qwen3-VL-Embedding文本输入、1024维；Milvus COSINE/FLAT；校验模型空间与版本 | [检索](../scripts/search_test_recipes.py)、[版本管理](../scripts/knowledge_versions.py)、[运行检查](../scripts/project_doctor.py) | 已入库2074块；模型更换需重建空间 |
| 召回不准怎么办 | 分析入库、候选、排序、上下文、生成五层；向量＋BM25＋有限规则，以RRF融合 | [BM25/RRF](../scripts/lexical_retrieval.py)、[候选规则](../scripts/retrieval_candidates.py)、[检索消融](../data/processed/engineering-acceptance/retrieval-ablation.json) | 已实现；复杂食材约束和无菜名泛化待评估 |
| 为什么加Rerank | 对候选联合评分，提高排序；不能找回缺失候选；保留消融与耗时 | [重排](../scripts/rerank_test_recipes.py)、[10分类验收](../data/processed/stage-acceptance/20261013T123419Z-92d0709f/retrieval.json) | 已完成有限对照；不宣称全库质量增益 |
| 回答与原文依据 | 生成、引用真实性、支持性与完整性核验；失败时仅原文降级 | [生成与核验](../scripts/generate_test_answer.py)、[证据测试](../tests/test_answer_evidence.py)、[并行复核测试](../tests/test_qa_execution.py) | 已实现；模型复核仍可能误判 |
| Agent和工作流区别 | 外层模型动态选择三类工具；底层RAG是固定流程，实际执行由后端授权 | [单Agent](../scripts/recipe_agent.py)、[工具](../scripts/recipe_tools.py)、[Agent测试](../tests/test_recipe_agent.py)、[范围测试](../tests/test_agent_scope.py) | 已实现并限制重复、次数和请求范围 |
| LangChain | create_agent协调者＋问答／原文图片专员；LangGraph checkpoint隔离请求 | [多Agent](../scripts/langchain_multi_agent.py)、[专员测试](../tests/test_langchain_multi_agent.py)、[真实记录](langchain-multi-agent-v1.md) | 可选模式已实现；未证明优于单Agent |
| 并发高怎么办 | 持久队列、两任务槽、容量8、429、幂等；模型scope共享lease与RPM／预算 | [任务队列](../scripts/job_queue.py)、[运行治理](../scripts/service_runtime.py)、[并发测试](../tests/test_service_runtime.py)、[阶段记录](stage-acceptance-v1.md) | 本机治理已验证；供应商生产容量待压测 |
| 模型超时怎么办 | 提交起180秒总预算、有限重试、计数熔断、取消进程组、10秒降级预留 | [阶段执行](../scripts/ask_test_recipes.py)、[执行策略](../scripts/qa_execution.py)、[真实HTTP故障测试](../tests/test_engineering_integration.py) | 已实现并测试；无硬实时／上游撤销保证 |
| 可运行与演示 | check/start/demo入口；本地模式三层禁模型；独立环境副本安装与演示 | [统一入口](../scripts/project.py)、[交付测试](../tests/test_project_delivery.py)、[运行说明](project-run-delivery.md) | 本机复现完成；冷部署待验证 |

## 测试数字怎样理解

- 全项目278项测试，覆盖程序行为、来源规则、状态管理、故障与边界；不是278个独立真实模型问题。
- 全库372/372来源原文与查图检查通过，2074块来源一致；372个完整原文均可展示。具名BM25 Top20来源命中372/372，不是块级证据Recall。
- 10分类各一题真实Embedding与Rerank来源命中通过；类别有覆盖，样本仍少，而且题目含菜名。
- 三次真实问答，包括两个同时执行的请求，引用与助手原文复核通过。糖醋汁串行27.883秒、并发91.666秒，不够估计生产P95。
- 旧版6题检索消融、30题问答复测、Agent对照以及首轮失败均保留；不能选择补测成功后声称历史一次全通过。
- 独立venv安装首轮失败、补测通过；复用同机基础环境，不是全新机器部署成功。

证据索引：[最新验收汇总](../data/processed/stage-acceptance/summary.json)、[测试记录](../data/processed/stage-acceptance/unit-tests.json)、[工程实现与测试对应表](engineering-implementation-and-tests.md)。

## 本次完善了什么

1. 核对所列面试需求与代码、测试及真实记录，集中呈现完成度和边界。
2. 更新面试文档的最新278项测试与阶段进度，将228等数量明确为历史记录。
3. 修正熔断描述：实际是模型scope失败计数、成功清零、默认三次触发与30秒冷却；生成和复核共享同模型scope，不是按phase隔离或滑动窗口失败比例。
4. 补充完整请求追踪、分块token代理限制、RRF公式、块级和来源级指标区别、检索重排真实消融数值，以及LangChain与业务校验边界。
5. 补充任务槽与模型槽、checkpoint与任务队列、队列终态与业务降级、SDK重复重试和超时取消的细节。

本次未发现阻塞上述需求的功能缺口，因此没有为补充文档而改变已验证的在线链路，也未新增调用付费API的测试。文档更新后检查本地链接、引用记录和指标计算，并重跑现有回归；结果见[本次核对记录](../data/processed/interview-readiness-v1/review.json)。

## 面试演示顺序

1. 先用面试文档第1章介绍项目，按第2章区分离线入库与在线工具选择。
2. 打开糖醋汁原文和计算章节，展示副标题分块、来源行号和单位。
3. 打开真实检索及重排记录，说清正确文件、正确章节和最终完整回答三个层次。
4. 查看原文＋图片复合请求的Agent记录，展示工具参数、次数及来源，而非只展示模型自由文字。
5. 使用现有故障测试记录讲429、401、deadline与降级；用真实并发记录说明速度边界。

```sh
conda activate py3.12.11
python scripts/project.py check --live
# 默认不调用模型的本地模式，已有8080时使用8081。
python scripts/project.py start --port 8081
# 另一个终端：
python scripts/project.py demo --port 8081
```

## 后续阶段优先级

| 优先级 | 后续工作 | 验收标准 |
|---|---|---|
| 1 | 未参与开发的质量题集，特别是无菜名食材推荐与复杂否定 | 标注来源和章节；分别报告正确、漏答、合理拒答、误拒答、降级，保留所有失败 |
| 2 | 全新机器或CI冷部署 | 从无依赖／无缓存开始复现，记录Python、Docker、数据、配置与跨平台OCR条件 |
| 3 | 真实供应商更多并发档位与重复轮次 | 同时报接受率、拒绝率、完成吞吐、队列与模型阶段耗时，不用三样本承诺P95 |
| 4 | 确定公开部署需求后完善多用户和跨主机运行 | 明确鉴权、数据权限、TLS、队列与故障恢复目标，再验收HA／SLA |

上述事项是下一阶段，不影响当前学习MVP与面试链路演示。范围未固定前不提供虚假的“项目完成百分比”。
