"""Code-enforced policy: source allowlist before search, full-source post-check."""
import json
import os
import re
from contextvars import ContextVar
from contextlib import contextmanager
from functools import lru_cache
from check_embedding import ROOT
from preference_recommendation import exclusion_pattern,OPAQUE_BLEND

HARD=ContextVar('cookbuddy_hard',default=())
ALIASES={'芫荽':'香菜','花生米':'花生','落花生':'花生','西红柿':'番茄','小米辣':'辣椒','小米椒':'辣椒',
         '花生油':'花生','乳制品':'牛奶','纯素':'vegan','素食':'vegetarian','清真':'halal'}
FAMILY_PATTERNS={
    '海鲜':r'海鲜|鱼|虾|蟹|贝|蛤|蚬|蚌|蚝|牡蛎|鱿鱼|章鱼|墨鱼|海参',
    '坚果':r'花生|核桃|杏仁|腰果|榛子|松子|开心果|碧根果|夏威夷果',
    '大豆':r'黄豆|大豆|豆腐|豆浆|豆干|豆皮|酱油|生抽|老抽|豆瓣|豆豉',
    '牛奶':r'牛奶|奶粉|炼乳|乳酪|奶酪|黄油|奶油|乳糖|乳清|乳蛋白|酸奶',
    '小麦':r'小麦|面粉|全麦|面包|吐司|土司|面条|面筋|酱油|生抽|老抽',
}
def pattern(value):return FAMILY_PATTERNS.get(value,exclusion_pattern(value))
def normalize(value):
    if not isinstance(value,str):raise ValueError('memory_value_must_be_string')
    return ALIASES.get(value.strip(),value.strip())

def current():
    value=HARD.get()
    if value:return list(value)
    raw=os.environ.get('COOKBUDDY_HARD_CONTEXT')
    return json.loads(raw) if raw else []

@contextmanager
def policy(hard):
    token=HARD.set(tuple(hard))
    try:yield
    finally:HARD.reset(token)

def subprocess_env():return {'COOKBUDDY_HARD_CONTEXT':json.dumps(current(),ensure_ascii=False)}

@lru_cache(maxsize=4096)
def _read(path,mtime,size):return path.read_text()

def full_text(source):
    path=(ROOT/source).resolve()
    if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():return None
    stat=path.stat();return _read(path,stat.st_mtime_ns,stat.st_size)

def forbidden(hard):
    names=[];equipment=[];diet=[]
    for p in hard:
        dim=p['dimension'];value=normalize(p['value'])
        if dim in {'allergy','avoid_ingredient'} and p.get('polarity') in {'avoid','dislike','must'}:names.append(value)
        elif dim=='equipment' and not value.startswith('有'):equipment.append(value)
        elif dim=='diet_type':diet.append(value)
    if any(v in diet for v in ('vegetarian','vegan')):
        names+=['鸡肉','牛肉','猪肉','鱼肉','虾','鸭','鹅','羊肉','蛤蜊','蟹','蚝油','鱼露','明胶','肉汤','高汤','猪油','鸡精','火腿','培根','香肠']
    if 'vegan' in diet:names+=['鸡蛋','牛奶','蜂蜜']
    if 'halal' in diet:names+=['猪肉','猪油','培根','火腿','香肠','料酒','白酒','啤酒','葡萄酒']
    return list(dict.fromkeys(names)),equipment,diet

def source_allowed(source,hard):
    if not hard:return True
    text=full_text(source)
    if text is None:return False
    names,equipment,diet=forbidden(hard)
    if any(re.search(pattern(n),text) for n in names):return False
    # Unknown blends and animal product labels cannot demonstrate compliance.
    if names and re.search(OPAQUE_BLEND,text):return False
    if 'halal' in diet and re.search(r'鸡|鸭|鹅|牛|羊|肉',text):return False  # no certification metadata
    for item in equipment:
        absent=re.match(r'^(?:没有|不用|无)(.+)$',item)
        only=re.match(r'^(?:只有|仅)(.+)$',item)
        if absent:
            if absent[1] in text:return False
        elif only:
            appliances=('烤箱','微波炉','空气炸锅','高压锅','电饭煲','电磁炉','厨师机','面包机','电压力锅','电炖锅','破壁机','榨汁机','搅拌机','燃气灶','煤气灶','烧烤架')
            if any(tool not in only[1] and tool in text for tool in appliances):return False
        else:return False
    return True

def allowed_sources(chunks,hard):
    return sorted({r['source'] for r in chunks if source_allowed(r['source'],hard)})

def build_milvus_filter(hard,chunks=None):
    if not hard:return ''
    if chunks is None:
        from recipe_tools import load_chunks
        chunks=load_chunks()
    allowed=allowed_sources(chunks,hard)
    return 'source in '+json.dumps(allowed,ensure_ascii=False) if allowed else 'source == "__no_allowed_recipe__"'

def post_check(dishes,hard):
    good=[];bad=[]
    for row in dishes:
        (good if row.get('source') and source_allowed(row['source'],hard) else bad).append(row)
    return good,bad

def blocked_result(message='在你的限制下没有找到合适的菜谱。'):
    return {'status':'insufficient','short_answer':message,'sources':[],'evidence':[],'images':[],'recommendations':[],
            'answer_method':'hard_constraint_guard'}

def check_result(result,hard=None):
    hard=current() if hard is None else hard
    if not hard:return result
    # All answer/evidence/images must belong to allowed sources. Never keep a
    # summary that could still describe a removed dish.
    refs=[]
    for key in ('sources','evidence','images','recommendations'):
        refs.extend(r for r in result.get(key,[]) if isinstance(r,dict))
    if any(not r.get('source') or not source_allowed(r['source'],hard) for r in refs):return blocked_result()
    return result

def request_conflict(question,hard):
    names,_,_=forbidden(hard)
    for name in names:
        for match in re.finditer(r'(?:含|加|放|用|想吃|要吃).{0,6}?(?:'+pattern(name)+')',question):
            prefix=re.split(r'[，,。；;!?！？]|但是|不过|但',question[:match.start()])[-1][-6:]
            if not re.search(r'不|别|无|避免|排除',prefix):return True
    return False
