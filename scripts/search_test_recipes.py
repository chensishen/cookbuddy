"""输入中文问题，在当前知识库中扩大候选并按菜名补召回。"""

import os
import argparse
import json
import uuid
import math
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from check_embedding import ROOT, config
from insert_test_recipes import milvus_api
from rag_dataset import FOLDER, COLLECTION
from service_runtime import request_json
from lexical_retrieval import BM25, rrf_merge
from retrieval_candidates import match_titles, merge_candidates, ingredient_candidates


def main(question=None, *, category=None, max_difficulty=None, ingredients_any=None, ingredients_all=None):
    if question is None:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('question',nargs='*')
        parser.add_argument('--category')
        parser.add_argument('--max-difficulty',type=int)
        parser.add_argument('--ingredient-any',action='append')
        parser.add_argument('--ingredient-all',action='append')
        args=parser.parse_args()
        question=' '.join(args.question) if args.question else input('请输入菜谱问题：')
        category=args.category;max_difficulty=args.max_difficulty
        ingredients_any=args.ingredient_any;ingredients_all=args.ingredient_all
    question=question.strip()
    if not question:
        raise ValueError("问题不能为空。")
    folder = FOLDER
    metadata = json.loads((folder / "embedding-metadata.json").read_text(encoding="utf-8"))
    from cookbuddy.ltm.guard import current,build_milvus_filter,post_check
    chunks = [json.loads(line) for line in (folder / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    from search_filters import filter_chunks,current_filters,scalar_filter
    filters=current_filters()
    if category is not None:filters['category']=category
    if max_difficulty is not None:filters['max_difficulty']=max_difficulty
    if ingredients_any is not None:filters['ingredients_any']=ingredients_any
    if ingredients_all is not None:filters['ingredients_all']=ingredients_all
    category=filters.get('category');max_difficulty=filters.get('max_difficulty')
    chunks,metadata_filter,selected_metadata=filter_chunks(chunks,**filters)
    native_filter=scalar_filter(filters) if metadata.get('metadata_schema_version')==1 else ''
    hard=current();guard_filter=build_milvus_filter(hard,chunks)
    guard_filter=' and '.join('('+term+')' for term in (native_filter or metadata_filter,guard_filter) if term)
    if hard:chunks,_=post_check(chunks,hard)
    if not chunks:raise ValueError('no_allowed_recipes')
    allowed_sources={row['source'] for row in chunks}
    raw_milvus=milvus_api
    def guarded_milvus(operation,payload):
        if operation=='entities/search' and guard_filter:
            payload={**payload,'filter':'('+guard_filter+')'+(' and ('+payload['filter']+')' if payload.get('filter') else '')}
        result=raw_milvus(operation,payload)
        if operation=='entities/search' and hard:result,_=post_check(result,hard)
        if operation=='entities/search' and metadata_filter and any(row.get('source') not in allowed_sources for row in result):
            raise ValueError('检索结果违反元数据过滤条件')
        return result
    settings = config()
    model = settings.get("EMBEDDING_MODEL")
    dimensions = int(settings.get("EMBEDDING_DIMENSIONS", "1024"))
    base = settings.get("EMBEDDING_BASE_URL", "").rstrip("/")
    key = settings.get("EMBEDDING_API_KEY", "")
    if model != metadata["requested_model"] or dimensions != metadata["dimensions"]:
        raise ValueError("问题模型与已有菜谱向量的模型或维度不一致。")
    if not key or urlparse(base).scheme != "https" or base != metadata["api_base_url"]:
        raise ValueError("API 配置与菜谱生成配置不一致或缺少密钥。")
    collection = guarded_milvus("collections/describe", {"collectionName": COLLECTION})
    vector_field = next(field for field in collection["fields"] if field["name"] == "vector")
    if int(dict((item["key"], item["value"]) for item in vector_field["params"])["dim"]) != dimensions:
        raise ValueError("集合向量维度不一致。")
    request = Request(
        base + "/embeddings",
        data=json.dumps({"model": model, "input": question, "dimensions": dimensions, "encoding_format": "float"}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST",
    )
    embedding = request_json(request, timeout=45, phase="embedding", cache_namespace=metadata["source_sha256"])
    data = embedding.get("data", [])
    if len(data) != 1:
        raise ValueError("问题向量数量无效。")
    vector = data[0]["embedding"]
    if len(vector) != dimensions or not all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector
    ) or sum(x*x for x in vector) == 0:
        raise ValueError("问题向量无效。")
    top_k = min(int(os.environ.get("RAG_TOP_K","20")), len(chunks))
    if top_k < 1: raise ValueError("Top K 必须为正")
    hits = guarded_milvus("entities/search", {
        "collectionName": COLLECTION, "data": [vector], "annsField": "vector", "limit": top_k,
        "outputFields": ["chunk_id", "title", "section", "content", "source", "source_start_line", "source_end_line", "embedding_model"],
        "searchParams": {"metricType": "COSINE", "params": {}}, "consistencyLevel": "Strong",
    })
    if len(hits) != top_k or any(hit["embedding_model"] != model for hit in hits):
        raise ValueError("检索返回数量或模型信息不符合预期。")
    matched_titles = match_titles(question, {row["title"] for row in chunks})
    title_hits = []
    if matched_titles and os.environ.get("RAG_AUGMENT","1")=="1":
        title_count = sum(row["title"] in matched_titles for row in chunks)
        title_hits = guarded_milvus("entities/search", {
            "collectionName": COLLECTION, "data": [vector], "annsField": "vector",
            "filter": "title in " + json.dumps(matched_titles, ensure_ascii=False), "limit": title_count,
            "outputFields": ["chunk_id", "title", "section", "content", "source", "source_start_line", "source_end_line", "embedding_model"],
            "searchParams": {"metricType": "COSINE", "params": {}}, "consistencyLevel": "Strong",
        })
        if len(title_hits) != title_count or any(row["title"] not in matched_titles or row["embedding_model"] != model for row in title_hits):
            raise ValueError("菜名补召回返回结果不完整。")
    inventory_terms, ingredient_sources = ingredient_candidates(question, chunks)
    ingredient_hits = []
    if ingredient_sources and os.environ.get("RAG_AUGMENT","1")=="1":
        count = sum(row["source"] in ingredient_sources for row in chunks)
        ingredient_hits = guarded_milvus("entities/search", {
            "collectionName": COLLECTION, "data": [vector], "annsField": "vector",
            "filter": "source in " + json.dumps(ingredient_sources, ensure_ascii=False), "limit": count,
            "outputFields": ["chunk_id", "title", "section", "content", "source", "source_start_line", "source_end_line", "embedding_model"],
            "searchParams": {"metricType": "COSINE", "params": {}}, "consistencyLevel": "Strong",
        })
        if len(ingredient_hits) != count or any(row["source"] not in ingredient_sources or row["embedding_model"] != model for row in ingredient_hits):
            raise ValueError("食材补召回返回结果不完整。")
    retrieval_mode = os.environ.get('RAG_RETRIEVAL','hybrid')
    if retrieval_mode not in ('hybrid','vector'):raise ValueError('未知检索策略')
    lexical_hits=[]
    if retrieval_mode=='hybrid':
        lexical=BM25(chunks).search(question,top_k)
        if lexical:
            lexical_rows=guarded_milvus('entities/search',{
                'collectionName':COLLECTION,'data':[vector],'annsField':'vector',
                'filter':'chunk_id in '+json.dumps([r['chunk_id'] for r in lexical],ensure_ascii=False),
                'limit':len(lexical),
                'outputFields':['chunk_id','title','section','content','source','source_start_line','source_end_line','embedding_model'],
                'searchParams':{'metricType':'COSINE','params':{}},'consistencyLevel':'Strong'})
            lookup={r['chunk_id']:r for r in lexical_rows}
            if set(lookup)!=set(r['chunk_id'] for r in lexical):raise ValueError('词法候选来源不完整')
            lexical_hits=[{**lookup[r['chunk_id']],'bm25_score':r['bm25_score']} for r in lexical]
        hits=rrf_merge({'vector':hits,'title':title_hits,'ingredient':ingredient_hits,'bm25':lexical_hits})
    else:
        hits = merge_candidates(hits, title_hits, ingredient_hits)
    report = {"question": question, "collection": COLLECTION, "model": model, "dimensions": dimensions,
              "inventory_terms": inventory_terms, "ingredient_sources": ingredient_sources,
              "vector_top_k": top_k, "matched_titles": matched_titles, "candidate_count": len(hits),
              "retrieval_mode":retrieval_mode,"fusion":"RRF" if retrieval_mode=="hybrid" else None,
              "hard_filter_applied":bool(guard_filter),"allowed_source_count":len({r['source'] for r in chunks}),
              "metadata_filters":{"category":category,"max_difficulty":max_difficulty,**{k:v for k,v in filters.items() if k.startswith('ingredients_')}},
              "metadata_filter_applied":bool(metadata_filter),
              "metadata_filter_execution":"native_scalar_and_array" if native_filter else "source_allowlist" if metadata_filter else "none",
              "filtered_source_metadata":{source:value for source,value in selected_metadata.items() if source in allowed_sources},
              "metric": "COSINE", "query_format": "原始中文问题，未添加额外指令",
              "created_at": datetime.now(timezone.utc).isoformat(), "query_vector": vector, "results": hits}
    output = folder / "search-results"
    output.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    (output / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview = [f"# 向量检索结果\n\n问题：{question}\n\n集合：`{COLLECTION}`；向量 Top K：{top_k}；合并候选：{len(hits)}；相似度：COSINE。分数不是答案正确率。\n"]
    print(f"问题：{question}\n匹配菜名：{matched_titles}；合并候选：{len(hits)}")
    for rank, hit in enumerate(hits, 1):
        print(f"{rank}. {hit['title']} / {hit['section']}：{hit['distance']:.6f}")
        preview.append(f"\n## 第 {rank} 名：{hit['title']} / {hit['section']}\n\n相似度：{hit['distance']:.6f}\n\n"
                       f"原文：[查看来源]({(ROOT / hit['source']).as_posix()}:{hit['source_start_line']})（第 {hit['source_start_line']}–{hit['source_end_line']} 行）\n\n"
                       f"````markdown\n{hit['content']}\n````\n")
    path = output / f"{stamp}.md"
    path.write_text("".join(preview), encoding="utf-8")
    print(f"完整结果：{path}")
    return report


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(f"请求返回 HTTP {error.code}，请检查服务或 API 配置。", file=sys.stderr)
        sys.exit(1)
    except (URLError, TimeoutError):
        print("连接失败，请检查 Milvus 服务和 API 网络。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError, StopIteration):
        print("检索未完成：请检查问题、模型、维度和集合返回结果。", file=sys.stderr)
        sys.exit(1)
