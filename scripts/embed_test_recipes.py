"""生成测试菜谱向量并保存到本地，不写入 Milvus。"""

import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from check_embedding import ROOT, config
from service_runtime import request_json


def main():
    settings = config()
    key = settings.get("EMBEDDING_API_KEY", "")
    base_url = settings.get("EMBEDDING_BASE_URL", "").rstrip("/")
    model = settings.get("EMBEDDING_MODEL", "")
    dimensions = int(settings.get("EMBEDDING_DIMENSIONS", "1024"))
    if not key or not model or urlparse(base_url).scheme != "https" or dimensions <= 0:
        raise ValueError("本地配置不完整。")

    folder = ROOT / "data/processed/test_dishes"
    source = folder / "chunks.jsonl"
    chunks = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not chunks or len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("文本块为空或编号重复。")
    output = folder / "chunks-with-vectors.jsonl"
    if output.exists():
        raise ValueError("已有向量文件；为避免重复调用和覆盖，请先检查已有结果。")

    records = []
    norms = []
    for index, chunk in enumerate(chunks, 1):
        # text 包含菜名、章节和原文路径，与已确认的分块文件保持一致。
        payload = {"model": model, "input": chunk["text"], "dimensions": dimensions, "encoding_format": "float"}
        request = Request(
            base_url + "/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        result = request_json(request,timeout=45,phase="test_embedding",cache_namespace="test-dishes")
        data = result.get("data", [])
        if len(data) != 1:
            raise ValueError("未返回一条向量。")
        vector = data[0]["embedding"]
        if len(vector) != dimensions or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in vector
        ):
            raise ValueError("返回向量的维度或数值无效。")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("返回全零向量。")
        norms.append(norm)
        records.append({**chunk, "vector": vector})
        print(f"[{index}/{len(chunks)}] {chunk['title']} / {chunk['section']}：{len(vector)} 维", flush=True)

    temporary = output.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    temporary.replace(output)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "api_base_url": base_url,
        "requested_model": model,
        "dimensions": dimensions,
        "chunk_count": len(chunks),
        "input_field": "text",
        "input_format": "菜名、章节、原文路径 + Markdown 正文；图片仅保留引用",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "norm_min": min(norms),
        "norm_max": max(norms),
    }
    (folder / "embedding-metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已保存 {len(records)} 条向量：{output}")
    print("尚未写入 Milvus。")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(f"API 返回 HTTP {error.code}；请检查权限、余额和接口配置。", file=sys.stderr)
        sys.exit(1)
    except URLError:
        print("无法连接 API，请检查网络。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError, TimeoutError):
        print("未完成：配置、输入文件或向量返回结果不符合要求；已有向量文件不会被覆盖。", file=sys.stderr)
        sys.exit(1)
