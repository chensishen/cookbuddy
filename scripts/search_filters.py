"""从当前块提取保守元数据，将明确的检索条件投影为来源白名单。"""
import json
import re
import os
from contextvars import ContextVar
from contextlib import contextmanager
from recipe_catalog import build_catalog,CATEGORIES

FILTERS=ContextVar('recipe_search_filters',default=None)
DIFFICULTY_LIMIT_PATTERN=r'难度\s*(?:不超过|不高于|至多|最多|≤|<=)\s*([1-5])(?![\d.点])\s*(?:星|级)?(?!\s*[星级半\d.点/／])'


def ingredient_terms(values):
    from ingredient_recommendation import normalize_ingredient,TOOLS
    if not isinstance(values,list) or not 1<=len(values)<=16:raise ValueError('食材条件须为1至16项列表')
    if any(not isinstance(v,str) or not re.fullmatch(r'[\u4e00-\u9fff]{1,12}',v) or TOOLS.search(v) for v in values):
        raise ValueError('食材条件须为明确食材名称，不含数量或工具')
    return sorted({normalize_ingredient(value) for value in values})


def validate_filters(category=None,max_difficulty=None,ingredients_any=None,ingredients_all=None):
    if category is not None and (not isinstance(category,str) or category not in CATEGORIES):
        raise ValueError('未知菜谱分类')
    if max_difficulty is not None and (type(max_difficulty) is not int or not 1<=max_difficulty<=5):
        raise ValueError('难度上限须为1至5')
    for values in (ingredients_any,ingredients_all):
        if values is not None:ingredient_terms(values)


def explicit_question_filters(question,chunks=()):
    """只补明确推荐中的唯一分类/数字上限；命名菜谱、否定及多分类不猜测。"""
    from retrieval_candidates import match_titles
    if not re.search(r'推荐|检索|查找|寻找|只找',question):return {}
    if match_titles(question,{row['title'] for row in chunks}):return {}
    cleaned=re.sub(r'不超过|不高于','至多',question)
    if re.search(r'不|非|除|取消|无需|无须|不限',cleaned):return {}
    found=[key for key,label in CATEGORIES.items() if label in question]
    if len(found)>1:return {}
    result={'category':found[0]} if found else {}
    matches=list(re.finditer(DIFFICULTY_LIMIT_PATTERN,question))
    if len(matches)==1:result['max_difficulty']=int(matches[0][1])
    return result


def current_filters():
    value=FILTERS.get()
    if value is None:value=json.loads(os.environ.get('RECIPE_SEARCH_FILTERS','{}'))
    if not isinstance(value,dict) or set(value)-{'category','max_difficulty','ingredients_any','ingredients_all'}:raise ValueError('检索过滤参数无效')
    validate_filters(**value)
    return {key:ingredient_terms(item) if key.startswith('ingredients_') else item for key,item in value.items() if item is not None}


@contextmanager
def filter_policy(**filters):
    validate_filters(**filters)
    token=FILTERS.set({**current_filters(),**{key:value for key,value in filters.items() if value is not None}})
    try:yield
    finally:FILTERS.reset(token)


def scalar_filter(filters):
    validate_filters(**filters)
    terms=[]
    if filters.get('category') is not None:terms.append('category == '+json.dumps(filters['category']))
    if filters.get('max_difficulty') is not None:terms.append('difficulty >= 1 and difficulty <= '+str(filters['max_difficulty']))
    for key,operation in (('ingredients_any','array_contains_any'),('ingredients_all','array_contains_all')):
        if filters.get(key) is not None:terms.append(operation+'(ingredients, '+json.dumps(ingredient_terms(filters[key]),ensure_ascii=False)+')')
    return ' and '.join('('+term+')' for term in terms)


def filter_chunks(chunks,category=None,max_difficulty=None,ingredients_any=None,ingredients_all=None):
    validate_filters(category,max_difficulty,ingredients_any,ingredients_all)
    any_terms=set(ingredient_terms(ingredients_any)) if ingredients_any is not None else set()
    all_terms=set(ingredient_terms(ingredients_all)) if ingredients_all is not None else set()
    if category is None and max_difficulty is None and not any_terms and not all_terms:return chunks,'',{}
    catalog=build_catalog(chunks);introductions={}
    for row in chunks:
        if row['section']=='简介':introductions.setdefault(row['source'],[]).append(row)
    selected={}
    for item in catalog:
        values=[];fence=None
        for row in sorted(introductions.get(item['source'],[]),key=lambda r:r['source_start_line']):
            for line in row['content'].splitlines():
                marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
                if marker:
                    value=marker[1]
                    if fence is None:fence=value
                    elif value[0]==fence[0] and len(value)>=len(fence):fence=None
                    continue
                match=None if fence else re.fullmatch(r'\s*预估烹饪难度\s*[：:]\s*(★{1,5})\s*',line)
                if match:values.append(len(match[1]))
        difficulty=values[0] if len(values)==1 else 0
        if category is not None and item['category']!=category:continue
        # 未知难度不能当成简单菜，也不把冲突的多个声明择优解释。
        if max_difficulty is not None and not 1<=difficulty<=max_difficulty:continue
        ingredients=None
        if any_terms or all_terms:
            source_rows=[r for r in chunks if r['source']==item['source']]
            if all(r.get('metadata_schema_version')==1 for r in source_rows):ingredients=set(source_rows[0]['ingredients'])
            else:
                from ingredient_recommendation import ingredient_fields
                from check_embedding import ROOT
                ingredients,*_=ingredient_fields([r for r in source_rows if '必备原料' in r['section']],(ROOT/item['source']).read_text().splitlines())
            if any_terms and not ingredients & any_terms or not all_terms<=ingredients:continue
        selected[item['source']]={'category':item['category'],'difficulty':difficulty,
            **({'ingredients':sorted(ingredients)} if ingredients is not None else {})}
    expression='source in '+json.dumps(sorted(selected),ensure_ascii=False) if selected else 'source == "__no_allowed_recipe__"'
    return [r for r in chunks if r['source'] in selected],expression,selected
