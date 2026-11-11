"""对已保存的检索候选进行重排，不重新生成向量或修改集合。"""

from stage_diagnostics import record as stage_record,failure_reason
stage_record('imports')

from rag_dataset import FOLDER

import os
import json
import uuid
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from check_embedding import ROOT, config
from service_runtime import request_json, remaining, CircuitOpen, AdmissionTimeout

MODEL = "Qwen/Qwen3-Reranker-8B"
stage_record('data_load')


def bounded_rerank(request,candidates):
    admission_seconds=float(os.environ.get('RAG_RERANK_ADMISSION_SECONDS','0.5'))
    if not math.isfinite(admission_seconds) or not .05<=admission_seconds<=8:
        raise ValueError('重排排队预算须为0.05至8秒')
    try:
        return request_json(request,timeout=8,attempts=1,phase='rerank',cache_namespace='rerank-v2',admission_timeout=admission_seconds),None
    except HTTPError as error:
        if error.code not in (408,429,500,502,503,504):raise
        remaining()
        return {'results':[{'index':i,'relevance_score':len(candidates)-i} for i in range(len(candidates))]},'HTTP'+str(error.code)
    except (TimeoutError,URLError,CircuitOpen) as error:
        remaining()  # An expired or cancelled parent must still stop here.
        reason='admission_'+error.reason if isinstance(error,AdmissionTimeout) else type(error).__name__
        return {'results':[{'index':i,'relevance_score':len(candidates)-i} for i in range(len(candidates))]},reason


def main():
    if len(sys.argv) != 2:
        raise ValueError("请提供检索结果 JSON 文件路径。")
    source = Path(sys.argv[1]).resolve()
    search = json.loads(source.read_text(encoding="utf-8"))
    candidates = search["results"]
    original_candidate_count=len(candidates)
    from ingredient_recommendation import parse_inventory
    ingredient_scope=bool(parse_inventory(search['question']))
    if ingredient_scope:
        tables=[row for row in candidates if '必备原料' in row['section']]
        if tables:candidates=tables
    candidate_limit=int(os.environ.get('RAG_RERANK_CANDIDATES','10'))
    if not 1<=candidate_limit<=50:raise ValueError('重排候选上限须为1至50')
    candidates=candidates[:candidate_limit]
    folder = FOLDER
    chunks = {row["chunk_id"]: row for row in (
        json.loads(line) for line in (folder / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
    )}
    documents = []
    for hit in candidates:
        chunk = chunks[hit["chunk_id"]]
        if any(chunk[field] != hit[field] for field in ("title", "section", "source", "content")):
            raise ValueError("本地文本块与检索候选不一致。")
        documents.append(chunk["text"])
    stage_record('prepare')
    settings = config()
    base = settings.get("EMBEDDING_BASE_URL", "").rstrip("/")
    key = settings.get("EMBEDDING_API_KEY", "")
    if urlparse(base).scheme != "https" or not key or not candidates:
        raise ValueError("API 配置或候选无效。")
    request = Request(
        base + "/rerank",
        data=json.dumps({"model": MODEL, "query": search["question"], "documents": documents,
                         "top_n": len(documents), "return_documents": False}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST",
    )
    enabled=os.environ.get('RAG_RERANK','1')=='1'
    fallback_reason=None
    if enabled:
        result,fallback_reason=bounded_rerank(request,candidates)
    else:
        result={'results':[{'index':i,'relevance_score':row.get('fusion_score',row['distance'])} for i,row in enumerate(candidates)]}
    reranked = result["results"]
    if len(reranked) != len(documents) or {item["index"] for item in reranked} != set(range(len(documents))):
        raise ValueError("重排返回编号或数量异常。")
    if not all(isinstance(item["relevance_score"], (int, float)) and not isinstance(item["relevance_score"], bool)
               and math.isfinite(item["relevance_score"]) for item in reranked):
        raise ValueError("重排分数无效。")
    reranked.sort(key=lambda item: item["relevance_score"], reverse=True)
    selected = reranked[:max(1,int(os.environ.get("RAG_TOP_N","5")))]
    rows = [{**candidates[item["index"]], "original_rank": item["index"] + 1,
             "rerank_rank": rank, "rerank_score": item["relevance_score"]}
            for rank, item in enumerate(selected, 1)]
    output = folder / "rerank-results"
    output.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    report = {"question": search["question"], "source_search": str(source.relative_to(ROOT)),
              "candidate_scope":"ingredient_tables" if ingredient_scope else "all_sections",
              "original_candidate_count":original_candidate_count,
              "candidate_limit":candidate_limit,
              "rerank_model": MODEL if enabled and not fallback_reason else "hybrid-order-fallback" if fallback_reason else "disabled-for-ablation", "rerank_enabled":enabled and not bool(fallback_reason), "rerank_fallback":fallback_reason, "candidate_count": len(documents), "selected_count": len(rows),
              "all_ranked_candidates": [{"chunk_id": candidates[item["index"]]["chunk_id"], "rerank_score": item["relevance_score"]} for item in reranked], "input_field": "text",
              "created_at": datetime.now(timezone.utc).isoformat(), "results": rows,
              "api_meta": result.get("meta")}
    (output / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview = [f"# 重排结果\n\n问题：{search['question']}\n\n排序方式：`{report["rerank_model"]}`。仅对原检索的 {len(documents)} 个候选重排，保留前 {len(rows)} 个。\n\n"
               "向量相似度与重排分数来自不同模型，不能直接比较；分数不是答案正确率。\n\n"
               "| 重排排名 | 原排名 | 菜名 / 章节 | 向量相似度 | 重排分数 |\n|---|---|---|---|---|\n"]
    print(f"问题：{search['question']}\n重排模型：{MODEL}")
    for row in rows:
        preview.append(f"| {row['rerank_rank']} | {row['original_rank']} | {row['title']} / {row['section']} | {row['distance']:.6f} | {row['rerank_score']:.6f} |\n")
        print(f"{row['rerank_rank']}. {row['title']} / {row['section']}：{row['rerank_score']:.6f}（原第 {row['original_rank']} 名）")
    for row in rows:
        preview.append(f"\n## 第 {row['rerank_rank']} 名：{row['title']} / {row['section']}\n\n"
                       f"原文：[查看来源]({(ROOT / row['source']).as_posix()}:{row['source_start_line']})\n\n"
                       f"````markdown\n{row['content']}\n````\n")
    path = output / f"{stamp}.md"
    path.write_text("".join(preview), encoding="utf-8")
    print(f"完整结果：{path}")
    stage_record('finished')


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        stage_record('finalize',failure_reason(error),error.code)
        print(f"Rerank 返回 HTTP {error.code}；请检查权限、余额或接口配置。", file=sys.stderr)
        sys.exit(1)
    except (URLError, TimeoutError) as error:
        stage_record('finalize',failure_reason(error))
        print("Rerank 连接失败，请检查网络。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError) as error:
        stage_record('finalize','result_validation')
        print("未完成重排：请检查输入文件、候选正文和接口返回结果。", file=sys.stderr)
        sys.exit(1)
