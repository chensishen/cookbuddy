# 菜谱问答服务恢复 · 2026-10-15

## 故障与处理

直接执行 search_test_recipes.py 失败，原 milvus-standalone 容器退出码134，日志出现 etcdserver: leader changed 后 panic；再次启动原容器仍立即退出。菜谱链路在检索阶段失败，所以系统只能展示原文候选。此处确认的是服务启动崩溃，尚未证明旧etcd内部状态异常的具体诱因。

原容器和 recipe-milvus-data 卷均保留，没有删除或覆盖。增加 Compose recipe-milvus 服务，使用独立 cookbuddy-recipes 数据卷；绑定127.0.0.1:19530与健康检查端口9094，配置健康检查和 unless-stopped 自动重启。记忆向量服务19532、其他项目19531和用户数据库不变。

从本地 bounded_v3 的2074条已生成向量恢复 recipe_bounded_v3 集合，复用全部向量，不调用Embedding重建。knowledge_versions.clone_index 对来源指纹、元数据、文本和每条向量逐条读回核对，全部2074条通过。新服务重启后查询仍为2074条，验证持久化。主动版本配置和集合名未切换。

## 实测

“基础牛奶面包的酵头怎么制作？”完整经过向量检索、重排、回答生成和原文校验，返回 status=verified、answer_method=model_generation，自动关联3张原图。生成答案包括混合原料、静置、发酵时间与原文引用，不再是 source_only_fallback。

回执：data/processed/cookbuddy-acceptance/rag-recovery-20261015.json。

## 运维

```sh
docker compose --env-file .env -f deployment/cookbuddy/compose.yaml up -d
docker compose --env-file .env -f deployment/cookbuddy/compose.yaml ps recipe-milvus
curl --max-time 3 http://127.0.0.1:9094/healthz
```

若数据卷仍在，启动无需重建索引。全新部署缺少集合时，可执行：

```sh
PYTHONPATH=scripts /opt/anaconda3/envs/py3.12.11/bin/python -c "from knowledge_versions import clone_index; from check_embedding import ROOT; print(clone_index('recipe_bounded_v3', folder=ROOT/'data/processed/recipe_versions/bounded_v3'))"
```

恢复命令拒绝覆盖已存在集合，不应为重复导入而删除现有集合。旧容器仍保留用于后续分析，其端口由新的本地服务接管。外部模型/API仍可能超时，知识不足或原文校验失败时仍应保留诚实的降级提示，不能承诺所有问题都生成答案。旧对话中的降级结果不会自动重写，应重新提问。

页面复测也返回 completed / verified 和3张图片，耗时约63秒，主要位于生成与原文校验阶段。此性能限制与本轮修复的检索服务故障分开记录。截图：data/processed/cookbuddy-acceptance/rag-recovered.png。
