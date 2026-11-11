# 自由表达与长对话改造验收（2026-10-27）

后续更新：模型已恢复，旧冷却记录已按成功探测结果解除；真实浏览器复验又发现并修复了组合请求、替换格式及章节绑定等冲突。当前结果见[模型恢复后的复验](live-dialogue-recovery-20261027.md)。下文保留恢复前的验收状态与证据。

实现与本地回归已完成。真实模型的自由表达验收受到上游429限流阻塞，尚未完成；现有结果不能证明任意表达或任意长度对话全面稳定。

## 实现

在线调用链统一为：规范化 → 一次结构化意图理解 → `src/dialogue.py` 状态迁移 → 来源与约束校验后的工具执行 → 持久化。执行器消费理解结果，不再另做一轮关键词意图改写。理解服务失败时，规则仅作兜底；仍无法可靠处理则明确说明服务不可用，并保留原话与上下文。

- 删除 `src/nlu/adapter.py`；删除旧 `scripts/cookbuddy/pipeline.py` 内的独立编排，仅保留统一引擎入口；`src/agent/legacy_bridge.py` 更名为 `src/agent/job.py`；删除 `--legacy` 和冗余 `--langchain` 启动切换。
- 选菜条件、食材增删与清空、推荐列表、序号指代、当前菜谱及当前章节分别持久化，不依赖无限增长的聊天文本。模型上下文采用摘要、结构化槽位与最近两轮。
- 图片、章节、原料追问绑定当前菜谱来源；章节图片之后继续问“具体怎么做”，继承当前章节。明确的新菜名优先于旧引用；确有多义才澄清。
- 推荐数量、定向换菜、排除条件、分类及难度过滤随状态传递；非法数量不破坏之前条件。未能核实的限制保留并澄清，不能静默忽略。
- 整餐规划接收结构化人数、时间、荤素及菜汤数量，避免执行阶段再用固定句式解析模型已理解的输入。
- 保留事实来源核实、硬约束、鉴权、PostgreSQL RLS 与记忆核心机制。提供数据库与鉴权专项回归；这不等于穷尽所有潜在冲突。
- 上游限流与真实语义不明确分别诊断；已知冷却期间不追加无效重试。服务降级提示不再把理解服务故障描述成“没理解你的意思”。耗时展示与历史恢复保持可见。

## 实际验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 默认后端回归 | 852项发现，810项通过、42项跳过 | [backend.log](../output/dialogue-refactor-20261027/backend.log) |
| 真实数据库、记忆与鉴权专项 | 40项通过；与默认回归有重叠，不相加为独立总数 | [postgres.log](../output/dialogue-refactor-20261027/postgres.log) |
| 前端渲染与图片开关 | 36项通过 | [frontend.log](../output/dialogue-refactor-20261027/frontend.log) |
| 长对话状态回归 | 100次状态迁移，含有界历史及序列化恢复 | `tests/test_dialogue_state.py` |
| 用户Chrome连续对话 | 8轮全部显示耗时，重载历史后仍可见 | [页面文本](../output/dialogue-refactor-20261027/browser-long-dialogue.txt) |

100次状态回归使用模拟的结构化理解结果或规则兜底，执行真实状态与工具逻辑；不是100次真实模型请求，也不是100轮浏览器验收。真实数据库专项使用实际数据库与工具，但不依赖外部理解模型。

Chrome实际复验“查看基础牛奶面包的完整原文和图片” → “只看酵头制作的图片”：返回3张加载成功的酵头图片，分别为“发酵前酵头的理想状态”“用布盖上酵头”“酵头的最终形态”。保存当前章节后再次追问“具体怎么做”，返回酵头制作章节。身份问答也正确返回能力介绍。以上发生于理解服务降级期间，不代表真实模型抽取质量已验收。

浏览器中的口语食材推荐仍因模型不可用而未完成，初次失败记录保留。修复诊断后，同一句问题明确显示理解服务暂不可用，并保留菜谱与食材状态。见[限流提示截图](../output/dialogue-refactor-20261027/browser-provider-limited.png)。

复跑命令（使用项目Python环境）：

```sh
PYTHONPATH=scripts:tests python -m unittest discover -s tests -q
COOKBUDDY_INTEGRATION_TESTS=1 PYTHONPATH=scripts:tests python -m unittest test_recommendation_turn.TaskStatePersistenceTests test_session_memory.SessionPersistenceTests test_task_context.TaskPersistenceTests test_cookbuddy_memory_regressions.MemoryOrderingTests memory.test_live_integration.LiveMemoryTests test_cookbuddy_http -q
node --test tests/test_answer_rendering.cjs tests/test_image_toggle.cjs
```

## 外部阻塞与待验收项

已记录上游模型HTTP429。共享冷却记录根据该响应的 `Retry-After`，当前可重试时间为 **2026-10-27 18:33:07，中国标准时间（UTC+8）**；这是供应商给出的重试时间，不保证到时恢复。没有清除冷却、绕过限额或伪造在线通过结果。

待供应商可用后，需用真实模型复验口语、错字、自由改条件、多候选指代与长链对话，检查理解准确率和是否按请求数量返回菜品。当前完成的是架构改造、故障处理及有明确范围的回归验证，尚不构成“全面稳定”的在线验收。

## 保留与审阅

本轮改造前快照：[dialogue-before-20261027.tar.gz](../output/backups/dialogue-before-20261027.tar.gz)，包含 `src`、`scripts`、`config`、`tests`、`docs`，不包含 `app`、`.env` 或数据库。旧代码已退出当前运行链路，快照用于审阅与恢复。

主要生产文件与该快照的比较见 [changes.diff](../output/dialogue-refactor-20261027/changes.diff) 与 [changes.json](../output/dialogue-refactor-20261027/changes.json)。这份比较覆盖列出的后端文件，不包含前端降级提示和本次文档修改；仓库尚无提交，不能把它描述成已提交的Git版本。
