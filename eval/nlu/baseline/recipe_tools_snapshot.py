"""菜谱Agent工具白名单：复用已校验RAG，读取原文与图片来源。"""
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path

from check_embedding import ROOT
from rag_dataset import FOLDER,DATASET
from ask_test_recipes import run_stage, StageFailure
from service_runtime import remaining, BudgetExceeded
from lexical_retrieval import BM25
import subprocess
import os
import copy
from process_recipe_media import local_file, reference_locations
from retrieval_candidates import match_titles
from reviewed_media_text import load_reviewed_texts
import chunk_test_recipes as splitter
from recipe_catalog import CATEGORIES


@lru_cache(maxsize=4)
def _load_chunks(path, stamp, size):
    return tuple(json.loads(line) for line in Path(path).read_text().splitlines())


def load_chunks():
    path=FOLDER/'chunks.jsonl';stat=path.stat()
    return list(_load_chunks(str(path),stat.st_mtime_ns,stat.st_size))


def resolve_title(title, chunks):
    titles={row['title'] for row in chunks}
    explicit={r['source'] for r in chunks if r['source'] in title}
    if len(explicit)==1:
        source=next(iter(explicit));name=next(r['title'] for r in chunks if r['source']==source)
        rest=title.replace(source,'')
        if any(other!=name for other in match_titles(rest,titles)):return None
        return name,source
    if len(explicit)>1:return None
    names=[title] if title in titles else match_titles(title,titles)
    if len(names) != 1:
        return None
    sources={r['source'] for r in chunks if r['title']==names[0]}
    if len(sources)!=1:
        explicit=[source for source in sources if source in title]
        if len(explicit)!=1:return None
        return names[0],explicit[0]
    return names[0],sources.pop()


def ambiguity_options(question,chunks):
    names=match_titles(question,{r['title'] for r in chunks})
    if len(names)!=1:return []
    sources=sorted({r['source'] for r in chunks if r['title']==names[0]})
    return sources if len(sources)>1 and not resolve_title(question,chunks) else []


def clarification(question,chunks=None):
    chunks=load_chunks() if chunks is None else chunks
    sources=ambiguity_options(question,chunks)
    if not sources:return None
    from recipe_catalog import build_catalog
    choices=[item for item in build_catalog(chunks) if item['source'] in sources]
    return {'status':'insufficient','short_answer':'找到同名菜谱的不同来源，不能自动混合配方。请选择一个来源继续，或查看各份原文后再决定。',
            'source_choices':choices,
            'evidence':[],'images':[],'sources':[{'title':'同名菜谱候选','section':'来源待选择','source':source,'line':1} for source in sources],
            'validation':'先明确来源，不凭相似度自动选择同名配方。'}


def checked_chunks(title):
    chunks=load_chunks(); resolved=resolve_title(title,chunks)
    if not resolved:return []
    name,source=resolved;path=local_file(source)
    splitter.SOURCE=ROOT/'data/HowToCook/dishes' if DATASET=='all_recipes' else ROOT/'data/HowToCook/test_dishes'
    current=splitter.split_recipe(path)
    metadata=json.loads((FOLDER/'embedding-metadata.json').read_text())
    if metadata.get('splitter_version')=='bounded-v2':
        from document_ingestion import bounded_chunks
        current=[{**piece,'parent_start':row['source_start_line'],'parent_end':row['source_end_line']}
                 for row in current for piece in bounded_chunks(
                     {**row,'locator_type':'line'},metadata['max_tokens_proxy'])]
        original=path.read_text().splitlines()
        for row in current:
            lines=row['content'].splitlines()
            starts=[i for i in range(row['parent_start']-1,row['parent_end']) if original[i:i+len(lines)]==lines]
            # 与导入一样只在已知父章节内定位，避免别的章节重复文字造成误拒答。
            if len(starts)!=1:raise ValueError('当前子块无法唯一定位原文')
            row['source_start_line']=starts[0]+1;row['source_end_line']=starts[0]+len(lines)
    selected=[r for r in chunks if r['source']==source]
    fields=('title','section','content','source_start_line','source_end_line')
    if len(selected)!=len(current) or not all(any(all(row[k]==new[k] for k in fields)
                                                  for new in current) for row in selected):
        raise ValueError('本地原文与知识库版本不同，请先核对数据版本')
    return selected


def unavailable(note):
    return {'status':'insufficient','short_answer':note,'evidence':[],'sources':[],'images':[]}


def images_in_evidence(images,evidence):
    """Keep the media bundle aligned with the exact source ranges being displayed."""
    return [image for image in images if any(
        row['source']==image['source'] and row['start_line']<=image['source_line']<=row['end_line']
        for row in evidence)]


def read_recipe(title, section=''):
    ambiguous=clarification(title)
    if ambiguous:return ambiguous
    rows=checked_chunks(title)
    if not rows:return unavailable('没有找到唯一匹配的菜谱，请提供库中完整菜名。')
    canonical=lambda text:re.sub(r'\s','',text).replace('的制作','制作')
    chosen=[r for r in rows if not section or canonical(section) in canonical(r['section'])]
    if not chosen:return unavailable('这份菜谱没有匹配章节。现有章节：'+'、'.join(r['section'] for r in rows))
    evidence=[];sources=[];remaining=14000
    for row in chosen:
        if remaining<=0:break
        # 只截在完整行边界，行号和摘录可以逐字回查。
        lines=(ROOT/row['source']).read_text().splitlines()
        extracted=[];start=row['source_start_line'];end=start-1
        for number in range(start,row['source_end_line']+1):
            line=lines[number-1]
            if len(line)+1>remaining:break
            extracted.append(line);remaining-=len(line)+1;end=number
        if not extracted:continue
        evidence.append({'quote':'\n'.join(extracted),'title':row['title'],'source':row['source'],'start_line':start,
                         'end_line':end,'section':row['section'],'evidence_type':'markdown_original'})
        sources.append({'title':row['title'],'section':row['section'],'source':row['source'],'line':start})
    complete=len(evidence)==len(chosen) and all(e['end_line']==r['source_end_line'] for e,r in zip(evidence,chosen))
    media=find_recipe_images(title,section,limit=None)
    return {'status':'verified_source','short_answer':'找到'+rows[0]['title']+'的原文'+('相关章节。' if section else '。')+
            ('' if complete else '内容较长，本次仅展示前面的完整原文行。'),
            'evidence':evidence,'sources':sources,'images':images_in_evidence(media.get('images',[]),evidence),'remote_links':media.get('remote_links',[]),'complete':complete,
            'validation':'逐字原文展示；不是模型生成的摘要或语义审核结论。'}


def find_recipe_images(title, section='', *, limit=8):
    ambiguous=clarification(title)
    if ambiguous:return ambiguous
    rows=checked_chunks(title)
    if not rows:return unavailable('没有找到唯一匹配的菜谱，请提供库中完整菜名。')
    source=rows[0]['source'];path=local_file(source);lines=path.read_text().splitlines()
    index=json.loads((ROOT/'data/processed/media_pilot/asset-index.json').read_text())
    reviewed=load_reviewed_texts()
    # Match a named chapter with optional Chinese possessive particles only.
    # Unknown chapter names still return no results.
    canonical=lambda text:re.sub(r'\s','',text).replace('的制作','制作')
    chapter=canonical(section)
    images=[]
    for asset in index['assets']:
        review=reviewed.get(asset['path'])
        if review and review['image_sha256'] != asset['sha256']:
            raise ValueError('人工核对文字与图片索引版本不一致')
        words=review['texts'] if review else []
        for ref in asset['references']:
            if ref['source'] != source or section and not (chapter in canonical(ref['section']) or section in ref['alt']
                    or any(section in word['text'] for word in words)):
                continue
            image=local_file(asset['path'])
            if (hashlib.sha256(image.read_bytes()).hexdigest()!=asset['sha256'] or
                hashlib.sha256(path.read_bytes()).hexdigest()!=ref['source_sha256'] or
                lines[ref['source_line']-1]!=ref['source_quote']):
                raise ValueError('图片或原文变化，旧关联不能沿用')
            images.append({'image_path':asset['path'],'image_sha256':asset['sha256'],**ref,
                           'evidence_type':'original_image_and_markdown_caption',
                           'reviewed_image_text':words})
    remote=[]
    for ref in index['remote_references']:
        if ref['recipe']==source:
            positions=reference_locations(lines,ref)
            if positions and (not section or section in ref['alt'] or any(section in lines[position-1] for position in positions)):
                remote.append({'target':ref['target'],'alt':ref['alt'],'source_line':positions[0],
                               'source':source,'status':'not_downloaded'})
    if not images:
        note='未找到匹配的本地图片。'
        if remote:note+=f'原文另有{len(remote)}处外部图片链接；图片未下载、内容未核验。'
        return {**unavailable(note),'remote_links':remote}
    images.sort(key=lambda image:image['source_line'])
    shown=images if limit is None else images[:limit]
    # Keep surrounding original steps so each verified image can be placed at its source line.
    surrounding=[]
    for chunk in rows:
        if any(chunk['source_start_line']<=image['source_line']<=chunk['source_end_line'] for image in shown):
            start=chunk['source_start_line'];end=chunk['source_end_line']
            surrounding.append({'quote':'\n'.join(lines[start-1:end]),'source':source,
                'start_line':start,'end_line':end,'section':chunk['section'],'evidence_type':'markdown_original'})
    # The limit selects context chunks; every image referenced inside those
    # chunks must accompany the text, even if this exceeds the preview limit.
    shown=images_in_evidence(images,surrounding) if surrounding else shown
    return {'status':'verified_source','short_answer':f"找到{len(images)}处本地图片引用，本次展示{len(shown)}处。图中文字仅用于找图；图注来自原文，照片不能单独证明温度、配比或熟度。",
        'images':shown,'remote_links':remote,'total_images':len(images),'complete':len(images)==len(shown),
        'evidence':surrounding or [{'quote':r['source_quote'],'source':r['source'],'start_line':r['source_line'],
                     'end_line':r['source_line'],'section':r['section'],'evidence_type':'markdown_image_reference'} for r in shown],
        'sources':[{'title':r['title'],'section':r['section'],'source':r['source'],'line':r['source_line']} for r in shown],
        'validation':'图片文件哈希、原文哈希与引用逐字核对；少量经助手看图核对的词仅用于找图，未作为配方事实。'}


def refresh_history_media(data):
    """Repair old source-view bundles without rewriting saved chat content."""
    result=copy.deepcopy(data);fresh={}
    for message in result.get('messages',[]):
        for output in (message.get('record') or {}).get('outputs',[]):
            old=output.get('result',{})
            if output.get('tool') not in ('read_recipe','find_recipe_images') or old.get('status')!='verified_source':continue
            evidence=old.get('evidence',[])
            images=[]
            try:
                for source in {row['source'] for row in evidence}:
                    lines=local_file(source).read_text().splitlines()
                    if any(row['quote']!='\n'.join(lines[row['start_line']-1:row['end_line']]) for row in evidence if row['source']==source):
                        raise ValueError('历史正文与当前来源不同')
                    if source not in fresh:fresh[source]=find_recipe_images(source,limit=None)
                    images.extend(images_in_evidence(fresh[source].get('images',[]),[row for row in evidence if row['source']==source]))
                old['images']=images
            except (ValueError,OSError,KeyError):
                old['images']=[]
                old['media']={'errors':[{'error_code':'historical_source_changed'}]}
    return result


def _query_recipes(question, progress=lambda message:None):
    progress('正在向量检索菜谱')
    search_path,search=run_stage('search_test_recipes.py',question,'search-results')
    if search['question']!=question:raise ValueError('检索问题不一致')
    progress('正在重排候选')
    rerank_path,rerank=run_stage('rerank_test_recipes.py',str(search_path),'rerank-results')
    if rerank['question']!=question or rerank['source_search']!=str(search_path.relative_to(ROOT)):
        raise ValueError('重排结果来源不一致')
    progress('正在生成回答与检查原文依据')
    answer_path,answer=run_stage('generate_test_answer.py',str(rerank_path),'answer-results')
    if answer['question']!=question or answer['source_rerank']!=str(rerank_path.relative_to(ROOT)):
        raise ValueError('回答结果来源不一致')
    if answer['status'] == 'validation_failed':
        original = read_recipe(question)
        if original['status'] == 'verified_source':
            return {**original, 'status': 'degraded',
                    'short_answer': '这次未能核实你所问的完整结论。请缩小到具体步骤或用量后再问；可在下方展开原文依据。',
                    'answer_method': 'validation_source_fallback',
                    'answer_result': str(answer_path.relative_to(ROOT)),
                    'stages': {'search': str(search_path.relative_to(ROOT)),
                               'rerank': str(rerank_path.relative_to(ROOT))}}
    sources=[{'reference_id':r['reference_id'],'title':r['title'],'section':r['section'],'source':r['source'],'line':r['source_start_line']}
             for r in answer['references'] if r['reference_id'] in answer['cited_reference_ids']]
    references={r['reference_id']:r for r in answer['references']}
    evidence=[{**item,'source':references[item['reference_id']]['source'],
               'title':references[item['reference_id']]['title'],
               'section':references[item['reference_id']]['section'],'evidence_type':'markdown_original'}
              for item in answer['evidence']]
    if answer['status']=='insufficient':
        import re
        named=re.fullmatch(r'(?:请问|请告诉我|请给我)?(.{2,24}?)(?:怎么做|的做法)[？?。！!]*',question.strip())
        if named and not match_titles(question,{r['title'] for r in load_chunks()}) and not any(
                named[1] in r['title'] for r in load_chunks()):
            answer['short_answer']='知识库未收录所问菜谱。'+answer['short_answer']
            answer['answer']='知识库未收录所问菜谱。'+answer['answer']
    return {'status':answer['status'],'short_answer':answer['short_answer'],'evidence':evidence,
            'sources':sources,'images':[],'display_blocks':answer.get('display_blocks',[]),'display_source':answer.get('display_source',[]),'answer':answer['answer'],'answer_method':answer['answer_method'],
            'answer_layout':answer.get('answer_layout'),'audit_notes':answer.get('audit_notes',[]),
            'validation':answer['validation'],'answer_result':str(answer_path.relative_to(ROOT)),
            'stages':{'search':str(search_path.relative_to(ROOT)),'rerank':str(rerank_path.relative_to(ROOT))},
            'models':{'embedding':search['model'],'rerank':rerank['rerank_model'],'generation':answer['generation_model']}}


def source_candidates(question):
    """故障时仅返回逐字核对的本地来源候选，不提供未经校验的配方结论。"""
    from search_filters import filter_chunks,current_filters
    chunks,_,_=filter_chunks(load_chunks(),**current_filters());named=resolve_title(question,chunks)
    pool=[r for r in chunks if r['source']==named[1]] if named else chunks
    hits=BM25(pool).search(question,3)
    evidence=[];sources=[]
    for row in hits:
        original=local_file(row['source']).read_text().splitlines()
        lines=row['content'].splitlines()
        starts=[i for i in range(row['source_start_line']-1,row['source_end_line']) if original[i:i+len(lines)]==lines]
        if len(starts)!=1:continue
        ident=len(evidence)+1
        evidence.append({'reference_id':ident,'title':row['title'],'section':row['section'],'source':row['source'],
                         'quote':'\n'.join(lines),'start_line':starts[0]+1,'end_line':starts[0]+len(lines),
                         'evidence_type':'markdown_original'})
        sources.append({'reference_id':ident,'title':row['title'],'section':row['section'],'source':row['source'],'line':starts[0]+1})
    return {'status':'degraded','short_answer':'问答链路暂时未完成。以下仅为已逐字核对的原文候选，尚未生成或核验问题结论。',
            'sources':sources,'evidence':evidence,'images':[],'answer_method':'source_only_fallback',
            'validation':'仅核对候选与本地原文一致；未声称候选已回答问题。'}


def _query_recipes_with_cache(question, progress=lambda message:None):
    progress('正在检查菜名与食材匹配')
    from search_filters import filter_chunks,current_filters
    filters=current_filters()
    chunks,_,_=filter_chunks(load_chunks(),**filters)
    ambiguous=clarification(question,chunks) if filters else clarification(question)
    if ambiguous:return ambiguous
    from ingredient_recommendation import recommendation_answer
    from cookbuddy.ltm.guard import current,post_check
    if current():chunks,_=post_check(chunks,current())
    from recipe_fact_answer import source_fact
    fact=source_fact(question,chunks)
    if fact is not None:
        progress('已从当前原文核对所问步骤、用量与必要提醒')
        return fact
    recommendation=recommendation_answer(question,chunks)
    if recommendation is not None:
        progress('已完成原料表覆盖率与缺料核对')
        return recommendation
    from service_runtime import RuntimeStore
    from cookbuddy.ltm.guard import current
    enabled=os.environ.get('RECIPE_ANSWER_CACHE','0')=='1' and not current()
    store=RuntimeStore() if enabled else None
    key=None
    if enabled:
        metadata=json.loads((FOLDER/'embedding-metadata.json').read_text())
        code_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'scripts').glob('*.py')))).hexdigest()
        policy={k:os.environ.get(k,'') for k in ('RAG_RETRIEVAL','RAG_AUGMENT','RAG_TOP_K','RAG_TOP_N','RAG_RERANK','RAG_CONTEXT_COMPLETION','RECIPE_GENERATION_FALLBACK','RECIPE_QA_PROFILE')}
        from check_embedding import config
        settings=config()
        namespace=[question,filters,metadata,code_hash,policy,{k:settings.get(k) for k in ('EMBEDDING_BASE_URL','EMBEDDING_MODEL','EMBEDDING_DIMENSIONS','LLM_BASE_URL','LLM_MODEL')}]
        key=hashlib.sha256(json.dumps(namespace,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        cached=store.get('answer:'+key)
        if cached and cached['result'].get('status')=='verified':
            try:
                unchanged=all(hashlib.sha256(local_file(path).read_bytes()).hexdigest()==digest for path,digest in cached['sources'].items())
            except (OSError,ValueError):unchanged=False
            if unchanged and cached['sources']:
                remaining();progress('已找到同版本且来源未变的已核验回答')
                store.event('answer_cache','',0,0,'cache_hit')
                return {**cached['result'],'cache_hit':True}
    try:
        result=_query_recipes(question,progress)
        if enabled and result.get('status')=='verified' and result.get('evidence'):
            try:
                sources={r['source']:hashlib.sha256(local_file(r['source']).read_bytes()).hexdigest() for r in result['sources']}
                if sources:store.put('answer:'+key,{'result':result,'sources':sources})
            except (OSError,ValueError):pass
        return result
    except (StageFailure,subprocess.TimeoutExpired):
        remaining(.5)
        progress('问答暂时未完成，正在提供原文候选')
        return source_candidates(question)


def attach_recipe_media(result,progress=lambda message:None):
    """Enrich the selected text sources with verified local media, without another LLM call."""
    if result.get('answer_layout')=='inventory_check':
        return {**result,'images':[],'remote_links':[]}
    if result.get('status') in ('degraded','validation_failed','insufficient','tool_failed'):
        return {**result,'images':[],'remote_links':[]}
    if result.get('source_choices') or not result.get('sources'):return result
    progress('正在为搜索结果匹配原图与步骤')
    grouped={}
    for source in result['sources']:
        path=source.get('source')
        if path:grouped.setdefault(path,set()).add(source.get('section',''))
    recommendations=bool(result.get('recommendations'))
    source_display=bool(result.get('display_source')) and result.get('status')=='verified'
    structured=bool(result.get('display_blocks')) and result.get('status')=='verified' and not recommendations
    images=[];remote=[];errors=[];seen=set()
    for source,sections in list(grouped.items())[:4]:
        # Recommendations get representative recipe images; QA images follow retrieved sections.
        scopes=[''] if recommendations else sorted(sections)
        if structured:scopes=sorted({a['section'] for b in result['display_blocks'] for a in b['anchors'] if a['source']==source and b['kind'] in ('answer','step','note')})
        selected=[]
        for section in scopes if structured or source_display else scopes[:4]:
            try:
                media=find_recipe_images(source,section,limit=None) if structured or source_display else find_recipe_images(source,section)
                for image in media.get('images',[]):
                    key=(image['source'],image['source_line'],image['image_path'])
                    if image['source']!=source or key in seen:continue
                    seen.add(key);selected.append(image)
                for link in media.get('remote_links',[]):
                    if link.get('source')==source:remote.append(link)
            except (OSError,ValueError,KeyError) as error:
                errors.append({'source':source,'error_code':type(error).__name__})
        images.extend(selected if structured or source_display else selected[:1 if result.get('recommendation_kind')=='preference' else 2] if recommendations else selected[:4])
        if len(images)>=8 and not structured and not source_display:break
    if structured:
        from answer_blocks import bind_block_images
        images=bind_block_images(result['display_blocks'],images)[:8]
    elif source_display:images=images_in_evidence(images,result['display_source'])
    else:images=images[:8]
    progress('已完成图文关联：'+str(len(images))+' 张原图')
    return {**result,'images':images,'remote_links':list({r['target']:r for r in remote}.values()),
            'media':{'image_count':len(images),'errors':errors,'policy':'answer_blocks_with_source_lines' if structured else 'selected_sources_and_sections'}}


def query_recipes(question,progress=lambda message:None,*,category=None,max_difficulty=None,ingredients_any=None,ingredients_all=None):
    from search_filters import filter_policy,current_filters,filter_chunks,explicit_question_filters,validate_filters
    from cookbuddy.ltm.guard import blocked_result
    validate_filters(category,max_difficulty,ingredients_any,ingredients_all)
    inferred=explicit_question_filters(question,load_chunks())
    inherited=current_filters()
    provided={'category':category if category is not None else inherited.get('category'),
              'max_difficulty':max_difficulty if max_difficulty is not None else inherited.get('max_difficulty')}
    if any(provided[key] is not None and provided[key]!=value for key,value in inferred.items()):
        return {**blocked_result('工具参数与问题中明确的分类或难度条件冲突，请核对条件。'),'answer_method':'metadata_filter_guard'}
    category=category if category is not None else inferred.get('category')
    max_difficulty=max_difficulty if max_difficulty is not None else inferred.get('max_difficulty')
    with filter_policy(category=category,max_difficulty=max_difficulty,ingredients_any=ingredients_any,ingredients_all=ingredients_all):
        filters=current_filters()
        chunks=load_chunks() if filters else []
        rows,_,_=filter_chunks(chunks,**filters) if filters else ([],None,None)
        named=resolve_title(question,chunks) if filters else None
        if filters and (not rows or named and named[1] not in {row['source'] for row in rows}):
            return {**blocked_result('在指定分类、难度及食材条件下没有找到可核实的菜谱。'),
                    'answer_method':'metadata_filter_guard','filters':filters}
        result=_query_recipes_checked(question,progress)
        if filters:
            allowed={row['source'] for row in rows}
            refs=[row for key in ('sources','evidence','images','recommendations','source_choices','display_source')
                  for row in result.get(key,[]) if isinstance(row,dict)]
            refs.extend(anchor for block in result.get('display_blocks',[]) for anchor in block.get('anchors',[]))
            if (any(row.get('source') not in allowed for row in refs)
                    or result.get('status') in ('verified','verified_source') and not refs):
                result={**blocked_result('回答来源不符合指定检索条件，暂不能提供该结论。'),
                        'answer_method':'metadata_filter_guard'}
            result={**result,'filters':filters}
        if inferred:result={**result,'explicit_question_filters':inferred}
        return result


def _query_recipes_checked(question,progress=lambda message:None):
    # Media is checked on every request, including text-cache hits: do not serve stale image hashes.
    from cookbuddy.ltm.guard import check_result,current,request_conflict,blocked_result,source_allowed
    hard=current();named=resolve_title(question,load_chunks())
    if hard and (request_conflict(question,hard) or named and not source_allowed(named[1],hard)):
        return blocked_result('这道菜或所要求的食材与已设置的硬限制冲突，暂不能推荐；可以选择其他菜谱。')
    raw=attach_recipe_media(_query_recipes_with_cache(question,progress),progress)
    checked=check_result(raw,hard)
    if hard and checked is not raw and checked.get('answer_method')=='hard_constraint_guard':
        # A source may have changed since retrieval. Recall under the refreshed
        # allowlist once; never retain prose describing a removed candidate.
        raw=attach_recipe_media(_query_recipes_with_cache(question,progress),progress)
        checked=check_result(raw,hard)
    return checked


QUESTION_GUIDANCE='question必须逐字复制用户的完整原始问题，包含否定、取消限制、过敏、数量及标点；不得摘要、纠正或删去前缀。取消的条件不填入过滤参数。'


TOOLS=[{'type':'function','function':{'name':name,'description':description,
    'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}
    for name,description,properties,required in (
        ('query_recipes','根据用户原始问题进行菜谱RAG查询或已有食材推荐，返回经来源检查的回答。不要删改库存、数量或否定条件。',
         {'question':{'type':'string','description':QUESTION_GUIDANCE},
          'category':{'type':'string','enum':list(CATEGORIES),'description':'仅填写用户明确要求的原库分类；未要求则省略'},
          'max_difficulty':{'type':'integer','minimum':1,'maximum':5,'description':'仅填写用户明确要求的难度上限；未要求则省略'},
          **{key:{'type':'array','minItems':1,'maxItems':16,'items':{'type':'string'},'description':description}
             for key,description in (('ingredients_any','用户明确要求菜谱至少包含这些食材之一时填写；普通已有库存不要转为硬条件'),
                 ('ingredients_all','用户明确要求菜谱必须包含全部这些食材时填写；普通已有库存不要转为硬条件'))}},['question']),
        ('read_recipe','按完整菜名查看菜谱原文，可指定章节。',
         {'title':{'type':'string'},'section':{'type':'string','description':'可选章节关键词，如操作、计算；不指定则空字符串'}},['title']),
        ('find_recipe_images','按菜名查原图与原文图注，可按章节、图注或少量已核对的图中文字过滤。图中文字仅用于找图，不是配方事实。',
         {'title':{'type':'string'},'section':{'type':'string','description':'可选章节、图注或图片中文字，如酵头、成品、预热；不指定则空字符串'}},['title']))]


def _dispatch(name, arguments, progress=lambda message:None, original_question=None):
    schema=next((t['function']['parameters'] for t in TOOLS if t['function']['name']==name),None)
    if schema is None:raise ValueError('未知工具')
    if not isinstance(arguments,dict) or set(arguments)-set(schema['properties']) or any(
            k not in arguments for k in schema['required']):raise ValueError('工具参数不符合定义')
    for key,value in arguments.items():
        if key in ('ingredients_any','ingredients_all'):
            from search_filters import ingredient_terms
            ingredient_terms(value)
            continue
        if key=='max_difficulty':
            if type(value) is not int or not 1<=value<=5:raise ValueError('难度上限须为1至5')
            continue
        if not isinstance(value,str) or len(value)>2000 or key in schema['required'] and not value.strip():
            raise ValueError('工具参数为空、过长或类型错误')
    if name=='query_recipes':return query_recipes(original_question or arguments['question'],progress,
        **{key:value for key,value in arguments.items() if key!='question'})
    if name=='read_recipe':return read_recipe(**arguments)
    return find_recipe_images(**arguments)


def dispatch(name,arguments,progress=lambda message:None,original_question=None):
    from cookbuddy.ltm.guard import check_result
    from cookbuddy.ltm.tools_memory import TOOLS as MEMORY_TOOLS,call
    if name in {t['function']['name'] for t in MEMORY_TOOLS}:
        result=call(name,arguments)
        return {**result,'status':'verified_source','short_answer':result['message'],'sources':[],'evidence':[],'images':[],'answer_method':'user_memory_tool'}
    return check_result(_dispatch(name,arguments,progress,original_question))
