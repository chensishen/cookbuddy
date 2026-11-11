# 全量菜谱知识库

## 导入范围与格式

导入 `data/HowToCook/dishes` 下的 372 篇正式菜谱，含饮品、调料等分类，按 Markdown 副标题切分为 2,058 块。保留简介、章节层级、菜名、原文路径和原文行号。最长正文 3,802 字节，本次无需额外拆短。

排除贡献模板 `dishes/template`、重复测试副本 `test_dishes`、README 索引、tips 烹饪知识、工程配置及 `.DS_Store`。tips 可作为以后单独讨论的数据源。

向量沿用当前本地配置：Qwen/Qwen3-VL-Embedding-8B，1,024 维。独立集合 `recipe_all_v1`，COSINE / FLAT，保留 `recipe_test_v1`。这一规模先沿用已验证的索引，避免同时改变分块、模型和索引后无法定位差异。

## 图片与其他附件

- 保留正式菜谱目录中的 354 个本地图片原文件，不改变格式、压缩或移动位置。
- Markdown 中的图片引用、替代文字保留在对应文本块中；附件清单另外记录菜谱路径、图片链接、是否存在、本地文件大小和 SHA-256。
- 351 次图片引用中有 2 个远程链接。本次保留链接，不下载；远程内容可能失效。其余本地引用核对无缺失，8 张本地图片未被 Markdown 引用，保留但不额外创建检索块。
- 初次全量导入没有 OCR、视觉描述或图片向量。Embedding 接收到的是文字；模型名称含 VL 不代表程序已经读取图片。后续已完成[三张图片的本地解析试验](recipe-media-processing.md)，另存OCR与看图描述草稿，尚未进入正式问答。
- 图片原文件适合继续存本地目录；以后服务部署时可存对象存储。Milvus 存文本、路径、元数据和向量，不存图片二进制。
- 后续如需要图片参与检索，可先抽样：对配方截图做 OCR，对步骤/成品照片生成视觉描述，将结果单独分块并标记来自图片、关联菜谱和图片路径。检查准确性后再考虑图片向量；单独的图片向量不会自动让现有文字问答理解图片。
- 仓库中的 JSON、锁文件和配置不作为菜谱正文；现有正式菜谱附件均为图片。以后新增 PDF/视频等需各自解析并保留文件及页码/时间位置，不能仅凭文件路径当作已解析内容。

硅基流动 [Embedding 官方文档](https://docs.siliconflow.cn/docs/api/embeddings-post) 支持 VL 模型接收图片 URL/base64；本次仅采用已验证的单文本请求。

## 使用与记录

全量导入成功并逐条核对后，`data/processed/active-dataset.json` 将默认问答切换到全量集合，检索、重排、回答使用同一份全量分块文件。

```bash
python3 scripts/ask_test_recipes.py "红烧肉需要哪些食材？"
```

临时切回原测试库：

```bash
RAG_DATASET=test_dishes python3 scripts/ask_test_recipes.py "西红柿炒鸡蛋应该先炒什么？"
```

数据与结果位于 `data/processed/all_recipes`：

- `chunks.jsonl`：分块正文及来源。
- `asset-manifest.json`：图片文件和菜谱引用关系。
- `preparation-summary.json`：范围、排除项与输入摘要。
- `vector-cache`：按块持久保存的向量，重跑复用已完成结果。
- `embedding-metadata.json`、`chunks-with-vectors.jsonl`：模型信息与完整向量。
- `import-state.json`：写入进度。
- `milvus-import-result.json`：总数、所有正文/来源/向量逐条核对结果。
- `search-results`、`rerank-results`、`answer-results`、`qa-results`：后续查询记录。

可续跑导入：

```bash
python3 scripts/import_all_recipes.py
```

配置或源文件改变时脚本停止，避免混用旧向量；同版本重跑用主键 upsert 避免重复记录。四路单文本请求，遇到限流或临时故障有限次重试，已完成向量缓存保留。

检索已改为向量 Top K 20（测试库只有 13 块时取 13）。问题包含完整菜名时，按该菜名从 Milvus 补入所有章节；与向量候选按 chunk_id 去重，保留实际相似度和召回渠道，再用 Qwen/Qwen3-Reranker-8B 重排后取前 5 个。输入未出现的菜名不强行匹配，同名菜谱来源不合并；目前不支持别名、错别字识别。结果文件使用时间戳加随机编号，避免并行调用覆盖。

仍沿用已有回答核对流程。全量导入完成不代表全量问答质量已验证：菜谱相似、章节分散、多食材推荐等仍需抽样评估，再共同讨论扩大召回或按菜谱补全章节。


## 食材推荐基础比较

明确的“我有/我只有”简单食材清单增加按原料表匹配的补召回。基本推荐由程序比较原料表，保留必备、可选、条件配料原文标记；只推荐一道。复杂忌口、用量和步骤仍走模型。当前不核对计算/操作章节新增配料与数量，回答会明确此限制。详见 `data/processed/all_recipes/ingredient-recommendation-report.md`。


## 同菜谱配料补查

基础食材推荐现在补齐所选菜谱同一来源路径的全部章节，计算列表中的表外条目单列差异，操作中识别到的用料仅标为待确认，保留引用与条件，不直接归为全部必需。原料词表与有限规则可能漏识或误识，数量和替代关系尚未自动验证。最新验证见 `data/processed/all_recipes/recipe-crosscheck-report.md`；之前原料表比较的限制已由本次补查部分改善。
