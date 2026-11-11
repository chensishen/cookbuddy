# 五篇结构化候选覆盖完成与阶段边界

2026-10-12。在[两篇修订候选](structured-repair-pilot-v1.md)之外，为此前模型抽取未完成的空气炸锅面包片、西红柿炒鸡蛋、凉皮建立三篇**原文优先的助手候选**。没有把历史失败草稿冒充新模型结果，也没有再次请求抽取接口。五篇候选的来源、统计和启用状态见 [`five-candidate-audit.json`](../data/structured/repaired_pilot/five-candidate-audit.json)。

| 菜谱 | 来源方式 | 本批重点 |
|---|---|---|
| 可乐鸡翅 | 旧模型草稿的独立修订 | 生姜误报、实际生抽数量差异、料酒或啤酒替代、步骤用水与油 |
| 蒸水蛋 | 旧模型草稿的独立修订 | 热水/温水与蒸锅水分用途、盘子工具、可选佐料和新鲜鸡蛋条件 |
| 空气炸锅面包片 | 原文优先建立 | 两片面包与空气炸锅分开；200°C、5分钟和营养表数值不变成采购食材 |
| 西红柿炒鸡蛋 | 原文优先建立 | 份数公式保留；糖、葱花、醋、番茄酱、清水和附加版本生抽不作为基础必备 |
| 凉皮 | 原文优先建立 | 组合清单拆成独立原料、调料和工具；绿豆芽纳入；不同步骤用水分开；辣椒油可选 |

五篇合计74个实体、176次章节观察。当前菜谱文件 SHA-256、逐字引用、0.2格式与预设关键边界全部通过；原模型草稿修订还核对其原始草稿哈希。随后运行[7组隔离库存对照](../data/evaluation/structured-five-isolated-v1.json)：空气炸锅面包片虽匹配面包片名称，仍显示空气炸锅工具未评估；西红柿炒鸡蛋的可选添加项不列为缺料；凉皮缺少的调料与可选辣椒油分列。全部对照的 `readiness=not_assessed`。

复现：

```sh
python3 scripts/curate_structured_drafts.py
python3 scripts/build_source_first_pilot.py
python3 scripts/evaluate_five_structured_pilots.py
python3 scripts/audit_five_structured_candidates.py
python3 -m unittest discover -s tests
```

本批项目174项测试通过。五篇候选仍 `activation.enabled=false`，没有进入正式 RAG、Agent 推荐或 Milvus 向量库；数量换算、温度、工具充足性、跨章节冲突及复杂版本没有获得全字段人工验收。这一批完成的是**五篇来源可检查的数据覆盖和隔离对照**，不是自动抽取模型恢复成功，也不是全库结构化质量证明。结构化试验到此可先收束，后续根据实际使用中的阻塞问题再决定是否扩大抽取或接入特定已核对字段。
