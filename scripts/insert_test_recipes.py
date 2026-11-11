"""将已生成的测试菜谱向量写入独立 Milvus 集合并核对。"""

import hashlib
import json
import math
from pathlib import Path
from urllib.request import Request
from pooled_http import urlopen

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:19530/v2/vectordb/"
COLLECTION = "recipe_test_v1"
from service_runtime import remaining


def milvus_api(operation, payload):
    request = Request(
        ENDPOINT + operation,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    def invoke(wait):
        with urlopen(request, timeout=wait) as response:result=json.load(response)
        if result.get('code')!=0:raise RuntimeError('milvus_operation_failed')
        return result.get('data')
    from service_runtime import guarded_call
    return guarded_call(invoke,phase='milvus',scope=ENDPOINT,timeout=remaining(30),attempts=1)


def main():
    folder = ROOT / "data/processed/test_dishes"
    metadata = json.loads((folder / "embedding-metadata.json").read_text(encoding="utf-8"))
    chunks = [json.loads(line) for line in (folder / "chunks-with-vectors.jsonl").read_text(encoding="utf-8").splitlines()]
    if hashlib.sha256((folder / "chunks.jsonl").read_bytes()).hexdigest() != metadata["source_sha256"]:
        raise ValueError("文本块已改变，请先核对向量版本。")
    if len(chunks) != metadata["chunk_count"] or len({c["chunk_id"] for c in chunks}) != len(chunks):
        raise ValueError("记录数量或编号无效。")
    dimensions = metadata["dimensions"]
    records = [{**chunk, "embedding_model": metadata["requested_model"]} for chunk in chunks]
    string_lengths = {
        "chunk_id": 1024, "title": 256, "section": 1024, "source": 2048,
        "content": 16000, "text": 20000, "embedding_model": 256,
    }
    for record in records:
        if any(len(record[name].encode("utf-8")) > limit for name, limit in string_lengths.items()):
            raise ValueError("字段超出最大字节数。")
        if len(record["vector"]) != dimensions or not all(math.isfinite(x) for x in record["vector"]):
            raise ValueError("向量维度或数值无效。")

    if COLLECTION in milvus_api("collections/list", {}):
        raise ValueError("测试集合已存在；停止执行以避免重复插入或覆盖。")
    fields = [
        {"fieldName": name, "dataType": "VarChar", "elementTypeParams": {"max_length": limit},
         **({"isPrimary": True} if name == "chunk_id" else {})}
        for name, limit in string_lengths.items()
    ]
    fields.extend({"fieldName": name, "dataType": "Int64"} for name in ("source_start_line", "source_end_line"))
    fields.append({"fieldName": "vector", "dataType": "FloatVector", "elementTypeParams": {"dim": str(dimensions)}})
    milvus_api("collections/create", {
        "collectionName": COLLECTION,
        "description": "HowToCook 测试菜谱；Markdown 副标题分块；Qwen3-VL-Embedding-8B / 1024",
        "schema": {"autoId": False, "enabledDynamicField": False, "fields": fields},
        "indexParams": [{"fieldName": "vector", "indexName": "vector_cosine", "metricType": "COSINE", "params": {"index_type": "FLAT"}}],
        "params": {"consistencyLevel": "Strong"},
    })
    print(f"已创建集合：{COLLECTION}（{dimensions} 维，COSINE / FLAT）", flush=True)
    inserted = milvus_api("entities/insert", {"collectionName": COLLECTION, "data": records})
    if inserted["insertCount"] != len(records):
        raise RuntimeError("插入数量不符合预期，请检查集合，勿重复插入。")
    print(f"写入返回：{inserted['insertCount']} 条", flush=True)
    milvus_api("collections/load", {"collectionName": COLLECTION})
    actual = milvus_api("entities/query", {
        "collectionName": COLLECTION, "filter": "", "limit": len(records) + 1,
        "outputFields": list(records[0]), "consistencyLevel": "Strong",
    })
    expected = {record["chunk_id"]: record for record in records}
    if len(actual) != len(records) or {row["chunk_id"] for row in actual} != set(expected):
        raise RuntimeError("查询到的记录数量或编号不一致。")
    for row in actual:
        record = expected[row["chunk_id"]]
        if any(row.get(field) != value for field, value in record.items() if field != "vector"):
            raise RuntimeError("正文或来源字段不一致。")
        if len(row["vector"]) != dimensions or not all(
            math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7) for a, b in zip(row["vector"], record["vector"])
        ):
            raise RuntimeError("向量核对未通过。")
    report = {
        "collection": COLLECTION, "database": "default", "verified_count": len(actual),
        "dimensions": dimensions, "metric": "COSINE", "index": "FLAT",
        "embedding_model": metadata["requested_model"], "fields_verified": True,
    }
    (folder / "milvus-import-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("核对通过：全部记录的正文、元数据和向量均与本地一致。")


if __name__ == "__main__":
    main()
