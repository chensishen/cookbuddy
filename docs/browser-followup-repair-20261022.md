# 浏览器第二轮问题修复与验收（2026-10-22）

本轮针对上一轮完整浏览器测试发现的8项问题完成修复，并在用户的真实Chrome中逐项复验。最终后端740项测试通过（真实PostgreSQL/Milvus集成开启，无跳过），前端29项通过。8080主页面、8082流式页面及5个项目容器保持运行。

## 原因、修复与浏览器证据

证据目录为 `output/browser-repair-20261022/`，下表文件均相对此目录。

| 问题 | 原因及修复 | 实际验收证据 |
|---|---|---|
| F1 首页整餐模板无法解析 | 人数后的“的”、时间前“有”、菜数前“想做”残留为未知条件；仅消化已识别语法，价格和未知限制仍不假核实 | `chrome-F1-meal.txt`：2人、30分钟、2菜1汤成功 |
| F2 临时忌口吞掉原文请求、污染画像 | 记忆管理匹配过宽，“不要放”抽取成“放鸡蛋”；先解析完整否定，配方要求保留为会话限制。与已核实原配方冲突时明确说明，禁止假称删去鸡蛋后效果已验证 | `chrome-F2-after-profile-repair.txt`：单条明确冲突说明，未删改原配方 |
| F3 替代题超时降级、同源重复卡片 | 子进程期限未服从工具期限；短生成预算被10秒后备预留挤占；单菜替代题规划反复取原文。统一阶段期限，在线后备预留2秒，明确单菜替代题单次受约束检索后生成与复核。同源摘录合并，完整原文优先，未核实结论显式提示 | `chrome-F3-substitution-final.txt`：18.0秒、单张回答，明确原文未说明橄榄油。是单次样本，非延迟分位数 |
| F4 换一道返回多道、丢失上下文 | 整句换菜意图识别不完整，拒绝菜名被误当新目标；共享完整意图解析、保留食材状态、数量1并排除已展示菜 | `chrome-F4-inventory.txt` → `chrome-F4-second-recipe.txt` → `chrome-F4-replacement-final.txt`：两道推荐、第二道全文、换成一道蛋煎糍粑，未返回炒滑蛋 |
| F5 章节图片查不到 | “酵头制作”与“酵头的制作”字面差异；仅规范已知制作连接词，明确章节图片走直接工具，未知章节仍返回不足 | `chrome-F5-chapter-main.txt`、`chrome-F5-F7-sse-final.txt`：两入口均3张酵头图片 |
| F6 依据区出现错误相对图片链接 | 普通Markdown图片进入超链接解析；统一安全渲染为原文配图图注，实际图片经已核实媒体入口显示 | `chrome-F6-evidence.txt`：配图图注，无图片伪链接 |
| F7 流式正文展示原始Markdown | SSE以纯文本累加；复用安全DOM渲染器，文本区独立重绘，不覆盖已到达图片 | `chrome-F7-original-sse-final.txt`：标题、嵌套列表、表格、强调及图注解析，图片与来源保留 |
| F8 推荐理由泛化 | 工具未应用当前登录用户画像，探索候选丢失解释；接入同一画像排序与有原文依据的理由。未核实匹配的候选明确标注为备选 | `chrome-F8-delivery-final.txt`：清蒸鲈鱼说明原文“清淡”匹配；响油鳝丝明确尚未核实偏好匹配 |

## 记忆修复

仅归档一条已证实由F2旧解析器误写的全局“放鸡蛋”记录，保留原始消息和审计事件。脚本 `scripts/repair_recipe_preference_scope.py` 默认只预览；必须同时满足精确原句、单次证据、单次会话、错误值和活动全局状态才处理，带版本检查且可重复运行。`legacy-preference-repair.json` 记录预览1、归档1、再次运行0。集成回归验证消息、审计及其他真实偏好保留。没有批量清空画像或删除历史会话。

## 验证与复跑

```sh
COOKBUDDY_INTEGRATION_TESTS=1 /opt/anaconda3/envs/py3.12.11/bin/python -m unittest discover -s tests
node --test tests/test_answer_rendering.cjs
```

最终结果分别在 `backend-delivery-acceptance.txt` 和 `frontend-final-acceptance.txt`。新增回归覆盖临时忌口、整餐语法、连续换菜、章节匹配、偏好隔离/理由、同源去重、子进程期限、生成预留、单菜替代路由、流式渲染和有审计的旧记录修复。

中间失败均保留：`chrome-F2-restriction.txt` 是早期重复不足回答；`chrome-F3-substitution-initial.txt` 是规划消耗预算的31秒安全降级；`live-answer-replay.json` 是诊断脚本遗漏scripts导入路径，修正后的 `live-answer-replay-2.json` 为10.004秒成功生成。`chrome-database-blocked.txt/.png` 是Docker管理员授权未完成时的阻塞；用户完成验证后恢复5个健康容器。`exploration-reason-red.txt` 保留新增理由回归的初始失败。历史 `data/processed/browser-repair-20261022/` 与 `output/browser-full-20261022/` 不变。

浏览器以原生Chrome输入、点击、可访问性树检查完成。截图工具本轮返回近空白图，因此不把该PNG当作图片像素加载或视觉布局通过的证据；实际章节图片节点、图注、语义排版和完成状态以Chrome文本快照为准，未宣称逐张像素验收或本轮浏览器控制台零错误。

## 运行边界

文字模型继续使用 `workbuddy-ai/deepseek-v4.1-flash` 与本机3425代理。Embedding、重排向量空间、30秒请求/8秒工具/20秒生成/2秒重排预算、并发、重试和M6六轮记忆窗口均未放宽。新增错误诊断仅输出限定分类，不泄露模型响应、密钥或连接串。不能唯一还原已丢失诊断信息的历史失败原因。

本轮8项修复验收通过，不等于全PRD重新封存验收或每次未来回答质量保证。价格预算仍缺来源；真人校准及成本可接受性仍待验收，配额/成本上限仍为“未定”。
