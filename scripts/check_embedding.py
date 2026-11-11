"""验证一次 Embedding 请求；不入库、不输出密钥或完整向量。"""

import json
import math
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def config():
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().strip("\"'")
    for key in ("EMBEDDING_API_KEY", "GITEE_AI_API_KEY", "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def main():
    settings = config()
    key = settings.get("EMBEDDING_API_KEY") or settings.get("GITEE_AI_API_KEY", "")
    if not key or set(key) <= {"X"}:
        raise ValueError("请先在项目 .env 中填写 EMBEDDING_API_KEY 并保存。")
    base_url = settings.get("EMBEDDING_BASE_URL", "https://ai.gitee.com/v1")
    if urlparse(base_url).scheme != "https":
        raise ValueError("Embedding API 地址需要使用 HTTPS。")
    dimensions = int(settings.get("EMBEDDING_DIMENSIONS", "1024"))
    payload = {
        "model": settings.get("EMBEDDING_MODEL", "Qwen3-Embedding-8B"),
        "input": "牛奶面包的酵头发酵失败怎么办？",
        "dimensions": dimensions,
        "encoding_format": "float",
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if urlparse(base_url).hostname == "ai.gitee.com":
        payload["instruction"] = "Given a recipe question, retrieve relevant recipe passages that answer the question."
        headers["X-Failover-Enabled"] = "true"
    request = Request(
        base_url.rstrip("/") + "/embeddings",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    from service_runtime import request_json
    result=request_json(request,timeout=60,phase="embedding_check")
    data = result.get("data", [])
    if len(data) != 1:
        raise ValueError("接口没有返回预期的一条向量。")
    vector = data[0].get("embedding", [])
    if len(vector) != dimensions:
        raise ValueError(f"请求 {dimensions} 维，实际返回 {len(vector)} 维；先不要入库。")
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
               and math.isfinite(x) for x in vector):
        raise ValueError("返回的向量包含无效数值。")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("接口返回了全零向量。")
    print("Embedding API 测试成功。")
    print(f"请求模型：{payload['model']}")
    print(f"返回向量维度：{len(vector)}")
    print(f"向量长度（L2 范数）：{norm:.6f}")
    print("尚未写入 Milvus。")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(f"API 返回 HTTP {error.code}。请检查密钥、模型权限、余额及接口配置。", file=sys.stderr)
        sys.exit(1)
    except URLError:
        print("无法连接 API，请检查网络与接口地址。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError, TimeoutError):
        # 不打印远端响应或异常正文，避免泄露密钥。
        print("测试未通过：请确认本地密钥已填写、配置有效，且接口返回有效的 1024 维向量。", file=sys.stderr)
        sys.exit(1)
