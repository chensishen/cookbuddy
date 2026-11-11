# 通用检索显式过滤（2026-10-20）

本步补齐PRD F4中的检索参数能力：通用检索新增可选 `category` 和 `max_difficulty`，在召回前限制来源，并校验返回范围。已有推荐的分类/难度过滤继续使用原入口。本步交付时仅含命令行和代码入口；后续Agent接入见 [Agent过滤记录](agent-search-filters-20261020.md)。完整PRD仍为部分通过。

## 问题与改动

此前通用检索只接收问题文本，分类、难度上限无法作为明确参数执行；来源硬限制已有独立治理。现在命令行与代码入口均支持这两个参数：

```sh
python scripts/search_test_recipes.py '鸡蛋汤怎么做' --category soup --max-difficulty 2
```

代码入口：`search_test_recipes.main(question, category='soup', max_difficulty=2)`。分类使用已有英文分类ID；难度上限仅接受整数1–5。未传条件时保留原有候选范围。

- 分类取当前块的来源目录；难度取同一批活动块“简介”中唯一的标准星级声明，排除代码围栏内容。缺失、多条声明或超出1–5星均视为未知；有难度上限时排除未知项。
- 先在当前块中选出符合条件的来源，再通过现有 `source` 标量生成Milvus白名单。向量、菜名、食材、BM25四条通道共同使用白名单；BM25也只在筛选后的块上建索引。
- 新条件与原有记忆硬限制取交集；没有合格来源时在Embedding和Milvus调用前停止，不放宽条件。Milvus返回范围外来源时拒绝结果。
- 检索报告增加 `metadata_filters`、`metadata_filter_applied`、`filtered_source_metadata`，便于复核。

代码：`scripts/search_filters.py`、`scripts/search_test_recipes.py`；新增9项测试位于 `tests/test_search_filters.py`。

## 验证

专项测试覆盖四路召回、分类与难度交集、未知/冲突难度、非法参数、空候选提前停止、硬限制交集、后端越界结果和命令行参数。

真实Milvus只读验证使用活动集合 `recipe_bounded_v3`：从2074块中筛出汤羹且难度不超过2星的10份来源、50块；四路召回返回34个候选，全部位于该来源范围。此验证复用一条现有文档向量，未向Embedding供应商发请求，仅验证过滤协议，不作为查询语义质量成绩。

证据：`data/processed/prd-acceptance/search-filters-20261020/local-milvus-search.json`；完整回归、代码哈希及日志见同目录 `summary.json`。本步未更新活动块、集合或向量，未重写封存评测。

## 剩余范围

这是现有来源标量上的过滤实现，尚未迁移生产集合的 `category`、`difficulty`、`ingredients` 独立字段，也未完成食材数组过滤或E6/A-4过滤收益消融。现有分类只是原库目录，不代表营养、素食或过敏安全认证；这些限制继续遵守已有硬约束检查。
