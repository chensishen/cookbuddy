# 理解层规则门盘点

范围：默认网页 / SSE、经典编排、原文与图片工具、推荐条件、记忆管理及事实输出。函数名用于稳定定位，具体行号见文末；重构前快照保存在本轮基线证据中。

| 位置（函数） | 原规则 / 作用 | 处置与保留理由 |
|---|---|---|
| src/api/chat_routes.py:ChatInput / actor | 长度、会话签名、同源、所有权 | 保留：输入边界与认证，不决定语义 |
| src/agent/engine.py:_direct | 日常对话固定句式 | 降级为无 NLU 结果时的兼容兜底；生产使用结构化模式 |
| src/agent/engine.py:_complete_scope | 无工具即强制检索 | 删除非菜谱模式下的补检索；仅已判定菜谱任务保留 |
| src/agent/engine.py:_expand | 关键词触发独立改写 | 新理解结果已有上下文改写时跳过，避免重复调用 |
| src/agent/turn_intents.py | 章节、图片、换菜整句正则 | 降级为模型失败的提示与兜底；程序只校验已解析序数 |
| scripts/langchain_service_worker.py:prepare | 规则先推进任务、再交模型 | 模型规范化路由前置；旧 reducer 作为后置确定性状态更新和失败兜底 |
| scripts/cookbuddy/router.py:local_recipe_intent / combine_scores | 关键词快路由、置信度门 | 经典入口接共享理解结果；原组合只留兼容入口 |
| scripts/cookbuddy/pipeline.py:_run | 未识别即 clarify、换菜无结果即终止 | 共享理解结果优先；未知条件转澄清/提示，不扩大到无条件推荐 |
| scripts/cookbuddy/task_context.py:advance_task | 未知片段转 extra_constraints | 保留无 NLU 调用者的原文条件；由理解适配器解释后再校验 |
| scripts/cookbuddy/recommendation_turn.py:interpret | 条件、库存、否定词语法 | 降级为兜底和确定性提议；完整原句始终保留，模型不决定权限 |
| scripts/preference_recommendation.py:recommend_preferences | extra_constraints 非空直接 None | 改为带选项的单问题澄清，不能静默丢弃限制或返回空 |
| scripts/recipe_tools.py:resolve_title | 唯一精确菜名 / 来源路径 | 分级名称匹配；来源歧义仍保留（防止混合配方） |
| scripts/recipe_tools.py:read_recipe | 章节字符串包含 | 分级章节匹配；低分返回候选，接近分数澄清 |
| scripts/recipe_tools.py:find_recipe_images | 章节 / 图注精确包含 | 章节共用容错匹配；图注和人工核对文字继续作为检索线索 |
| scripts/recipe_tools.py:checked_chunks | 文件版本、哈希、原文行号 | 保留：事实溯源与版本一致性 |
| scripts/ingredient_recommendation.py:normalize_ingredient / parse_inventory | 食材别名 / 库存语法 | 食材槽位使用共享匹配；原料表抽取仍确定性，不对原文作推测改写 |
| src/agent/tools.py:invoke_tool | 原始问句覆盖模型参数 / 章节原文限制 | 结构化理解优先；硬约束及原句同时保留，禁止无依据扩展 |
| scripts/langchain_service_worker.py:memory_authorized | 记住 / 忘记必须用户明确授权 | 保留：模型输出不能授权记忆修改 |
| scripts/cookbuddy/ltm/{guard,profile_store,management}.py | 过敏忌口过滤、确认、隔离 | 保留：安全硬约束和所有权，不因容错解除 |
| src/agent/runtime.py:guard | 最终来源白名单二次检查 | 保留：即使模型提议错误也不能绕过硬约束 |
| scripts/generate_test_answer.py:validate_answer | 原文引用、完整性、数值依据 | 保留：仅事实输出需要证据；通用对话不进入此分支 |
| scripts/search_filters.py / recipe_catalog.py | 分类、难度、来源白名单 | 保留：合法槽位校验和执行过滤 |
| src/agent/tools.py / middleware/ | 参数 schema、调用数量与预算 | 保留：资源边界和参数合法性，不用未知文本拒绝语义 |

日志仅允许意图、置信度、命中层级/分数、固定路径码、耗时、调用和 token 计数；问句、槽位文字、候选名称、模型原始输出不进入运行日志。会话持久化和评测夹具属于用户数据/测试证据，不属于运行日志。

## 当前代码行号

以下从最终代码的 Python AST 提取，对应上表规则。

- `src/api/chat_routes.py:21`：`ChatInput`
- `src/api/chat_routes.py:52`：`actor`
- `src/agent/engine.py:179`：`_direct`
- `src/agent/engine.py:264`：`_expand`
- `src/agent/engine.py:310`：`_complete_scope`
- `scripts/langchain_service_worker.py:32`：`prepare`
- `scripts/langchain_service_worker.py:203`：`memory_authorized`
- `scripts/cookbuddy/router.py:20`：`combine_scores`
- `scripts/cookbuddy/router.py:31`：`local_recipe_intent`
- `scripts/cookbuddy/pipeline.py:70`：`_run`
- `scripts/cookbuddy/task_context.py:71`：`advance_task`
- `scripts/cookbuddy/recommendation_turn.py:69`：`interpret`
- `scripts/preference_recommendation.py:43`：`recommend_preferences`
- `scripts/recipe_tools.py:35`：`resolve_title`
- `scripts/recipe_tools.py:78`：`checked_chunks`
- `scripts/recipe_tools.py:116`：`read_recipe`
- `scripts/recipe_tools.py:168`：`find_recipe_images`
- `scripts/ingredient_recommendation.py:12`：`normalize_ingredient`
- `scripts/ingredient_recommendation.py:21`：`parse_inventory`
- `src/agent/tools.py:75`：`invoke_tool`
- `scripts/cookbuddy/ltm/guard.py:61`：`source_allowed`
- `scripts/cookbuddy/ltm/guard.py:102`：`check_result`
- `scripts/cookbuddy/ltm/profile_store.py:29`：`connect`
- `scripts/cookbuddy/ltm/profile_store.py:67`：`apply`
- `scripts/cookbuddy/ltm/management.py:45`：`handle_command`
- `src/agent/runtime.py:70`：`guard`
- `scripts/generate_test_answer.py:162`：`validate_answer`
- `scripts/search_filters.py:21`：`validate_filters`
- `src/agent/middleware/guard_mw.py:17`：`awrap_tool_call`
- `src/agent/middleware/resilience_mw.py:14`：`awrap_model_call`
