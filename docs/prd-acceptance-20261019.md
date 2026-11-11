# PRD 整体改进与验收（2026-10-19）

依据：[PRD v1.1](../PRD_菜谱RAG_Agent.md)。结论：**部分通过，未达到完整 PRD 验收条件**。本轮完成服务恢复、解析补齐和评测口径修复；真实模型调用仍出现 HTTP 429，统计质量、人评、完整成本及部分元数据需求尚未通过。

## 本轮改进

1. **恢复部署**：两个 Milvus 原来均因 `panic: etcdserver: leader changed` 反复重启。将内嵌 etcd 拆成各自独立的服务，通过健康依赖控制 Milvus 启动顺序，保留原卷及 `/var/lib/milvus/etcd` 数据目录。操作前已备份两份 etcd 数据，备份目录已加入 Git 忽略。没有重建集合或改变向量空间。恢复后再次重启两个 Milvus，集合仍可加载。
2. **修正运行检查**：依赖检查能正确识别 `psycopg[binary]` 的发行包名；Docker 检查改为当前 compose 的五个容器，移除过时的容器名。
3. **补齐候选解析**：DOCX 保留 Heading 样式、标题路径与原始表格单元格；新增 XLSX 工作表、原始行范围、合并区域及公式单元格定位。公式保留为未计算文本，合并或公式表标记待复核。CSV 多行单元格保留换行。公式绑定相邻说明；未闭合公式/代码、损坏 DOCX、缺失文件被隔离，不中断整批处理。DOCX/PDF 直接转 IR，避免先经过旧分块而误拒大表格。
4. **限制重排输入**：默认仅重排前 30 个候选，可用 `RAG_RERANK_CANDIDATES` 设置 1–50；记录截断上限、真实重排和降级原因。
5. **修正评测可信度**：每轮独立运行库；检索失败纳入零分分母；仅真实重排进入配对检验；大样本采用固定种子的置换抽样，避免指数级枚举。报告固定题集路径及哈希；评审和 Ragas 读取同一版本。价格必须同时匹配模型与供应商，否则费用留空。评审缺失报告覆盖数；Agent 不可用计入失败，成功回答延迟与全部尝试延迟分别记录，防止把快速熔断误读为性能提升。
6. **更新回归断言**：原文快速回答已省去旧模型调用；相关测试改为检查所选菜谱、来源与逐字证据。超时注入测试显式关闭快速路径，确保确实验证远程阶段超时后的降级。

主要代码：[部署](../deployment/cookbuddy/compose.yaml)、[独立 etcd 配置](../deployment/cookbuddy/standaloneEtcd.yaml)、[运行检查](../scripts/project_doctor.py)、[文档 IR](../scripts/document_ir.py)、[DOCX 结构](../scripts/document_ingestion.py)、[重排](../scripts/rerank_test_recipes.py)、[评测](../scripts/evaluate_prd.py)、[后处理](../scripts/review_prd_eval.py)、[Ragas 适配](../scripts/evaluate_ragas.py)。

## 实际验证

- 完整回归 **512 项全部通过，无跳过**，启用了 `COOKBUDDY_INTEGRATION_TESTS=1`，覆盖真实 PostgreSQL；结果见[回归日志](../data/processed/prd-acceptance/checks-20261019/unit-tests.log)。`pip check` 无依赖冲突。这些测试不代表真实模型质量全部通过。
- 前端渲染 **25 项通过**，见[日志](../data/processed/prd-acceptance/checks-20261019/frontend-tests.log)。
- 活动库 **372/372 份来源可还原**，维持 2074 个活动块；另对同一批 372 个文件运行新候选解析，372 正常、0 隔离。该结果仅证明本批可解析，不能推断所有格式保真。见[候选报告](../data/processed/prd-acceptance/checks-20261019/parser-active-corpus.json)。未将候选发布到活动库。
- 目录分页、同名来源选择、逐字原文、图片归属、搜索分类及冲突来源拒绝，通过 `project.py demo` 验收，见[接口记录](../data/processed/run-delivery/20261019T143220Z-8ca9fa9a/local-flows.json)。
- 浏览器实测“简易红烧肉怎么做？”显示用量、编号步骤和可点击的菜谱/章节/路径，页面执行计时约 0.6 秒。这是已有原文快速路径的单次结果，不能当作模型问答延迟。见[页面证据](../data/processed/prd-acceptance/checks-20261019/browser-redbraised.png)。
- 浏览器独立对话实测“有鸡蛋和番茄能做什么？”返回三张候选卡，显示覆盖率、已有食材、待备原料和可选项；首项西红柿炒鸡蛋覆盖率为50%，明确待备盐及食用油。该场景在最终评测结束后单独完成，页面计时约14.4秒，不替换整组评测中该题的失败。见[页面证据](../data/processed/prd-acceptance/checks-20261019/browser-inventory.png)。
- 两个 Milvus 再次重启后，[检查结果](../data/processed/prd-acceptance/checks-20261019/health-after-restart.json)确认活动集合已加载、维度和数量一致，五个容器均健康，应用在本机 8080 端口运行。未进行生产负载测试。

## 真实在线评测

[原始报告](../data/processed/prd-acceptance/20261019T143428370388Z/report.json)、[可读报告](../data/processed/prd-acceptance/20261019T143428370388Z/report.md)、[运行事件汇总](../data/processed/prd-acceptance/20261019T143428370388Z/runtime-summary.json)、[make 日志](../data/processed/prd-acceptance/checks-20261019/make-eval.log)。

题集仍是已有 **10 题开发诊断集**：8 题检索，5 题做三组回答对照，不是独立 test。实时请求期间脚本起止哈希一致；后续统计修订由报告中的 `postprocessing_code_sha256` 单独追踪，没有替换任何原始回答。

| 检索方案 | 样本 | Hit@5 | MRR |
|---|---:|---:|---:|
| 纯向量、相同 H2 索引 | 8 | 62.5% | 0.3479 |
| BM25 | 8 | 75.0% | 0.6875 |
| 混合、无重排 | 8 | 100% | 0.8333 |
| 混合、真实重排 | 8 | 100% | 0.9063 |
| 去 BM25 | 8 | 75.0% | 0.7500 |

8/8 题都实际使用重排。相对纯向量，Hit@5 增加 37.5 个百分点；重排相对无重排的 MRR 提升 8.75%，配对检验 **p=1.0，不显著**，不能据此通过 PRD 的重排显著性验收。

当前聊天配置为 `agnes-3.0-flash`，实际调用出现 HTTP 429，随后触发熔断。完整 Agent 的 5 题中：1 题正常可读回答、3 题 Agent 不可用、1 题降级。库外两题均未完成合格拒答，本轮拒答正确率为 0/2。成功回答只有 1 条、耗时约 38.06 秒，不报告可靠的成功 P95；全部尝试 P50 约 0.005 秒来自快速失败，不能作为提速结论。

`make eval` 在实时评测阶段返回非零，后续 judge/Ragas 因依赖失败未执行；另行运行了不调用模型的报告后处理。Ragas 的供应商参数与题集版本适配已有代码和回归，但**未完成本轮真实 Ragas 评分**。旧 DeepSeek 价格与当前供应商不匹配，费用正确保留为空。此前共享状态诊断轮保存在 `20261019T142844375091Z/report.json`，已标记中断诊断，不计入最终统计。

## F1–F15 验收矩阵

| 需求 | 本轮判定 | 证据或剩余缺口 |
|---|---|---|
| F1 文档解析 | 部分通过 | 活动来源及候选解析通过；活动实体仍未齐备 difficulty、normalized ingredients 等标量 |
| F2 按章节分块 | 活动链路通过 | 2074 个块可还原；新 IR 原子公式及说明绑定已测试，候选未发布 |
| F3 Embedding 入库 | 现有部署通过 | 原集合恢复并通过重启检查；保留当前 Qwen 向量空间，未替换为 BGE-M3 |
| F4 检索与过滤 | 部分通过 | 向量与菜名/来源/硬约束过滤已有；通用分类、难度标量过滤尚未齐全 |
| F5 混合检索 | 开发集通过 | BM25 + dense + RRF；未迁移为 Milvus 原生 sparse |
| F6 CrossEncoder | 功能通过，统计验收未通过 | 8/8 实际重排，MRR 提升但 p=1.0 |
| F7 Agent 工具 | 部分通过 | 工具编排已有；本轮受 429/熔断影响，5 题仅 1 题正常回答 |
| F8 引用回答 | 部分通过 | 全库逐字来源还原和页面引用通过；98% 引用准确率与语义忠实度尚未独立验收 |
| F9 指标评测 | 开发诊断通过 | 规则指标、区间、失败分母和降级记录可复跑；非盲测质量结论 |
| F10 稳定性 | 部分通过 | 启动竞态修复、重启与故障回归通过；429 仍影响可用性，延迟目标未通过 |
| F11 多格式 | 部分通过 | MD/HTML/DOCX/PDF/CSV/TSV/XLSX 候选可用；复杂 PDF 版面、全面 OCR/VLM 未验收 |
| F12 表格/公式 | 部分通过 | 表头/行范围/表格单元格及公式说明绑定通过；跨页表格、表级摘要及合并表头自动理解仍待做 |
| F13 溯源 | 活动库通过，字段尚有缺口 | 372/372 来源及行号可还原；候选有哈希和解析器版本，全部 Milvus 实体的 commit/质量字段仍未齐全 |
| F14 质量分流 | 部分通过 | 异常文件隔离、去重和批次容错已测试；近重复、多类噪声收益实验未齐全 |
| F15 可复现消融 | 部分通过 | 三组对照、部分消融与失败证据可复跑；固定 512-token 基线、完整 E0–E8/A-1–A-6、独立 dev/test、人评校准、完整 Ragas/成本仍缺 |

## 复跑与剩余验收条件

```sh
docker compose --env-file .env -f deployment/cookbuddy/compose.yaml up -d
COOKBUDDY_INTEGRATION_TESTS=1 make test PYTHON=/opt/anaconda3/envs/py3.12.11/bin/python
node --test tests/test_answer_rendering.cjs
make eval PYTHON=/opt/anaconda3/envs/py3.12.11/bin/python
python scripts/project.py check --live
```

完整通过至少还需：解决当前供应商限流后重跑回答/拒答及 judge/Ragas；补齐活动实体和通用分类/难度过滤；建立经审核的独立 dev/test，完成指定基线、消融及人工校准；按当前供应商和完整调用链核算成本并验证延迟预算。本报告没有降低 PRD 阈值，也没有把外部失败或待评审项目记为通过。
