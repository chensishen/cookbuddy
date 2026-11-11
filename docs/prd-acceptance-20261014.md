# PRD 整体改进与验收（2026-10-14）

验收依据：[PRD v1.1](../PRD_菜谱RAG_Agent.md)。结论是**部分通过**：本地功能、DeepSeek接入与来源还原已验证；整体统计质量验收尚未完成。已有成熟链路保留，新解析管线作为隔离候选，不直接覆盖知识库。

## 本轮改进

- 聊天模型配置与Embedding配置拆分，生成、支持性与完整性复核、单Agent和LangChain多Agent统一使用 `deepseek-flash`。非思考模式采用DeepSeek官方 `thinking.type=disabled`，避免把其他供应商参数发给DeepSeek。真实JSON、工具调用及网页接口均已验证。
- 食材推荐增加同义词、用量备注清理、工具过滤与可选项分类；按可识别必备原料的覆盖率降序返回多道菜及未声明拥有的原料，不默认已有油盐。复杂否定、过敏、数量及替代继续走原有来源核对链路。
- 加入“番茄炒蛋→西红柿炒鸡蛋”菜名别名，保留原始用户问题。简单库存问题在重排前限定原料表范围；无重排消融采用相同范围，避免把过滤收益冒充CrossEncoder收益。
- 新增 `document_ir.py`：MD/HTML/CSV/TSV及已有DOCX/PDF适配、统一元素IR、行/页/节点定位、表格分组重复表头与列名值序列化、代码和公式原子性、质量分/重复检测/隔离报告。当前均为候选，`production_enabled=false`。
- 新增一键基线/消融评测、配置与源码/数据/来源commit哈希、分类指标、bootstrap区间、配对检验、用量和价格快照、同模型judge及Ragas忠实度评测。每轮独立目录保留全部失败；没有把补测成功替换首轮结果。

代码入口：[聊天配置](../scripts/llm_config.py)、[食材推荐](../scripts/ingredient_recommendation.py)、[统一候选IR](../scripts/document_ir.py)、[评测](../scripts/evaluate_prd.py)、[评审与成本](../scripts/review_prd_eval.py)、[Ragas](../scripts/evaluate_ragas.py)。

## 按PRD逐项验收

| 需求 | 状态 | 证据与边界 |
|---|---|---|
| F1 Markdown解析 | 部分通过 | 372来源的章节及原文已覆盖；活动索引未统一存储difficulty、normalized ingredients标量 |
| F2 结构分块 | 通过现有来源检查 | 2074章节/子块；新IR补原子代码/公式及表格专项处理，候选未发布；未证明参数最优 |
| F3 Embedding→Milvus | 通过已有部署 | 活动集合recipe_bounded_v3加载且维度/数量一致；保持现有Qwen文本向量空间，未按BGE-M3重建 |
| F4 语义检索与标量过滤 | 部分通过 | 向量检索、菜名/来源过滤已具备；完整category/difficulty/辣度等标量过滤未齐全 |
| F5 混合检索 | 通过开发集 | 中文BM25+稠密向量RRF；稀疏通道在本地计算，未使用Milvus原生sparse字段 |
| F6 CrossEncoder重排 | 功能通过、统计阈值待达标 | 真实供应商重排；小样本MRR改善不显著，不能声称全库增益 |
| F7 Agent工具调用 | 通过实际场景 | query_recipes/read_recipe/find_recipe_images；推荐由query_recipes接入，未独立拆分所有PRD示例工具 |
| F8 引用回答 | 部分通过 | 原文行号及逐字引用检查通过；每项结论语义支持的98%阈值未由独立盲测验证 |
| F9 评测指标 | 通过开发诊断 | Hit/Recall/Precision/MRR/nDCG及类别区间，答案judge单独报告，不等同人工准确率 |
| F10 稳定性 | 现有治理回归通过 | 有界队列、限流、超时重试熔断、缓存及降级已有测试；无新的生产容量结论 |
| F11 多格式 | 部分通过 | 新HTML/CSV/TSV及已有DOCX/PDF候选；XLSX、PDF复杂版面及全面OCR/VLM尚未验收 |
| F12 表格/公式 | 部分通过 | 重复表头、行范围、原子公式/代码；表级摘要、跨页表格、公式与变量描述绑定待完善 |
| F13 溯源 | 已有库通过 | 来源文件、行号可还原；新IR附来源哈希/解析器版本；未把commit/质量字段写进全部Milvus实体 |
| F14 质量处理 | 部分通过 | 新候选管线有评分/重复/隔离及报告；近重复和所有劣质文档类型未覆盖 |
| F15 可复现基线消融 | 部分通过 | make eval、三组回答基线、检索与BM25/重排消融、judge/Ragas已接入；开发集仅10题，未完成PRD 300–1000题dev/test分离、人评、全部Ragas指标与全部增量/留一消融 |

## 可复跑

```sh
conda activate py3.12.11
python -m venv .venv-ragas
.venv-ragas/bin/python -m pip install -r requirements-evaluation.lock
make test
make eval
# 仅离线来源与BM25检查，无远程请求：
make eval-offline
# 单独复跑最新报告的Ragas忠实度：
make eval-ragas
```

聊天密钥在本地`.env`，Git忽略且权限600。`LLM_BASE_URL=https://api.deepseek.com/v1`、`LLM_MODEL=deepseek-flash`。Embedding/Rerank沿用原密钥；运行检查分别验证两组配置。完整查询需Milvus和两组供应商接口可用，`.env.example`不包含密钥。

Ragas使用独立venv，避免改变应用的LangChain依赖。首次安装曾遇到Ragas与新版langchain-community移除vertexai适配的兼容问题；已固定兼容版本，独立环境pip check通过，完整解析依赖锁在requirements-evaluation.lock。Ragas评审与生成同用DeepSeek，存在同模型偏好。闭卷基线的忠实度用gold来源审计，它生成时未收到这些来源；其余方案用各自实际上下文。

所有延迟均为小样本、本地同版本缓存诊断，不能作生产P95承诺。成本提供LLM峰/谷价格上下界，依据2026-10-14的[官方价格快照](https://api-docs.deepseek.com/quick_start/pricing/)；不含Embedding/Rerank、重试失败计费和评审费用，不称完整请求成本。复杂食材表达、近义词歧义以及表外配料仍可能遗漏，推荐结果只承诺原料表范围比较。

## 保留证据

- [测试结果](../data/processed/prd-acceptance/unit-tests.json)
- [网页五场景验收](../data/evaluation/app-acceptance-v1/summary.json)
- [候选解析样例](../data/processed/prd-acceptance/parser-samples.json)
- [最新报告位置](../data/processed/prd-acceptance/latest.json)
- [首轮检索87.5%及所有回答记录](../data/processed/prd-acceptance/20261014T033114609630Z/report.md)
- [原料表范围修复后的整组复测](../data/processed/prd-acceptance/20261014T033852069435Z/report.md)

初始Ragas试跑有一个InstructorRetryException，旧推荐中工具被列为可选配料的问题也保留在首轮记录；工具词表已修正。派生覆盖率由程序计算，Ragas上下文包含该工具真实返回的派生数据，明确与原文区分，不能仅用原文表格评估包含计算结果的回答。

## 固定最终代码的完整复跑结果

[完整报告](../data/processed/prd-acceptance/20261014T034418682324Z/report.md)与[逐题JSON](../data/processed/prd-acceptance/20261014T034418682324Z/report.json)。这轮代码起止哈希一致，`make eval`包含实时检索/回答基线、自建judge及Ragas，命令完成；291项回归全部通过。来源审计372/372通过，网页五场景通过，DeepSeek多Agent原文+图片请求完成。

| 检索方案（8题） | Hit@5 | MRR |
|---|---:|---:|
| 纯向量Naive | 62.5% | 0.3479 |
| BM25 | 75.0% | 0.6875 |
| 混合/同范围过滤、无重排 | 100% | 0.8333 |
| 混合+CrossEncoder重排 | 100% | 0.9063 |
| 去掉BM25 | 75.0% | 0.7500 |

最终方案对纯向量Hit@5绝对提升37.5个百分点、相对提升60%。重排MRR相对提升8.75%，配对检验p=1.0，小样本下**没有统计显著性**，PRD该项未通过；不能因此称该组件在全库一定有收益。

五题回答对照（含两道库外题）中，同模型judge给Closed-book 4/5、Naive RAG 3/5、完整Agent 5/5。Naive的两道拒答输出为空，结构abstain标记不能替代用户可读的拒答。完整系统2/2库外题明确返回“知识库未收录”。闭卷温度题编出160–180°C，原文是200°C。完整系统逐字引用通过，仍不足以证明98%语义引用准确率。

完整Agent P50约25.87秒、P95约58.19秒；Naive P50约1.72秒、P95约3.13秒。完整Agent相对Naive的P95增加约55.06秒，**未达到PRD示例1.5秒延迟增量目标**；耗时包含本地共享模型限流等待，与前两轮存在明显波动，不能简单归因DeepSeek推理耗时。完整Agent平均每请求LLM费用上界约0.000730 USD，Naive约0.000194 USD；这不是完整系统账单。

Ragas忠实度完成9条评分，无执行失败：完整系统温度、菜名别名两题为1.0，食材推荐为0.6。该分数与自建judge的5/5发生分歧，必须保留并进一步校准，不能采用较高分宣称全部正确。闭卷按gold来源作事后审计，其上下文不是实际检索结果，不应用于闭卷检索质量比较。

最终判定：**部分通过，未达到PRD完整验收**。阻塞全面通过的主要项为：独立dev/test题集与人评不足、重排显著性未成立、食材推荐框架评分分歧、延迟增量超出示例阈值、完整成本与多格式部分要求待完善。

[另一次Ragas食材推荐诊断](../data/processed/prd-acceptance/20261014T034418682324Z/ragas-inventory-diagnostic.json)检出的不支持项为覆盖率计算范围与“仅比较原料表”的两条边界声明：评审认为上下文未明确写出这些规则。这提示框架对程序政策说明的评分口径需要校准；它不是首轮0.6分的原始解释，也不据此改写或删除首轮分数。
