"""按实际食材推荐；不推断步骤里的必需性、替代方案或库存用量。"""
import re
import json
from functools import lru_cache
from pathlib import Path,PurePosixPath
from check_embedding import ROOT

ALIASES = {'西红柿':'番茄', '马铃薯':'土豆', '食盐':'盐', '食用盐':'盐', '蒜':'大蒜'}
TOOLS = re.compile(r'锅|刀|铲|碗|盆|烤箱|冰箱|微波炉|面包机|空气炸锅|搅拌机|搅拌器|打蛋器|筛|厨房秤|量杯|烤盘|铝箔|温度计|计时器|秒表|电饭煲|筷|保鲜膜|锡纸|纱布')
COUNTED_FOOD=re.compile(r'^[一二两三四五六七八九十半]+(?:个|只|块|条|根|把)(?P<part>[\u4e00-\u9fff]{1,10}的)?')
COUNT_SUFFIX=re.compile(r'(?P<name>.+?)[一二两三四五六七八九十百半]+(?:到[一二两三四五六七八九十百半]+)?(?:个|只|块|条|根|把|片|瓣|颗|枚)(?:左右)?$')
SIZE_PREFIX=re.compile(r'^(?:比)?(?:拳头|鸡蛋)(?:大|小)(?:一点|一些)?的')
SIZE_SUFFIX=re.compile(r'(?:拳头大小|中等个头|大个头|小个头)+$')


def normalize_ingredient(raw,*,aliases=True):
    return ingredient_identity(raw,aliases=aliases)[0]


def ingredient_identity(raw,*,aliases=True):
    """Separate known count/size syntax from identity; retain its raw evidence."""
    text = re.sub(r'\[[^\]]+\]\([^)]*\)', lambda m: m[0].split(']')[0][1:], raw)
    text = re.sub(r'[（(][^）)]*[）)]', '', text).strip(' -*\t')
    text = re.sub(r'^\d+(?:\.\d+)?\s*(?:个|克|g|ml|毫升|枚|瓣|根)\s*', '', text, flags=re.I)
    text = COUNTED_FOOD.sub('',text,count=1)
    before=text
    count=COUNT_SUFFIX.fullmatch(text)
    if count:text=count['name']
    text=SIZE_PREFIX.sub('',text)
    text=SIZE_SUFFIX.sub('',text)
    quantity_detail=text!=before
    text = re.split(r'[：:=]|\d', text)[0].strip()
    text = re.sub(r'(?:适量|少许|若干|一个|两个)$', '', text).strip()
    if re.fullmatch(r'.{0,6}锅(?:中)?用水',text):text='水'
    return (ALIASES.get(text, text) if aliases else text),quantity_detail


def parse_inventory(question):
    # 仅接受明确库存；带复杂否定、忌口、数量的查询继续由原有 RAG 核对。
    if re.search(r'过敏|忌口|不吃|不能吃|替代|省略|不要|辣|清淡|素菜|难度|快手|没有|只有|数量|多少|怎么|如何|步骤', question):
        return None
    m = re.search(r'(?:我有|现有食材[：:]?|冰箱里有|有)\s*(.+?)(?=[，。；]|能做什么|可以做什么|推荐)', question)
    if not m or not re.search(r'能做什么|可以做什么|推荐', question):
        return None
    raw = re.split(r'、|和|及|与|,|，', m[1])
    if not raw or any(not re.fullmatch(r'[\u4e00-\u9fff]{1,10}', x.strip()) for x in raw):
        return None
    terms = list(dict.fromkeys(normalize_ingredient(x) for x in raw))
    if any(TOOLS.search(x) for x in terms):
        return None
    return terms


def recommendation_filters(question):
    """只识别明确分类与数字难度；其余限制仍交给完整问题核对。"""
    from recipe_catalog import CATEGORIES
    categories={label:key for key,label in CATEGORIES.items()}
    found=[(label,key) for label,key in categories.items() if label in question]
    from search_filters import DIFFICULTY_LIMIT_PATTERN
    difficulty=re.search(DIFFICULTY_LIMIT_PATTERN,question)
    if len(found)>1:return None
    cleaned=question
    if found:cleaned=cleaned.replace(found[0][0],'')
    if difficulty:cleaned=cleaned.replace(difficulty[0],'')
    # 不丢弃不辣、素食、过敏、其他难度表达等无法验证的用户约束。
    if re.search(r'不|无|非|只要|仅|难度|简单|容易|快手|分钟|素食',cleaned):return None
    if found or difficulty:
        inventory=re.search(r'(?:我有|现有食材[：:]?|冰箱里有|有)\s*(.+?)(?=[，。；]|能做什么|可以做什么|推荐)',cleaned)
        if not inventory:return None
        remainder=cleaned[:inventory.start()]+cleaned[inventory.end():]
        remainder=re.sub(r'能做什么|可以做什么|推荐|给我|[1-9一二两三四五六七八九]\s*(?:个|道|种)|菜谱|菜','',remainder)
        if remainder.strip('，。；、, \t\n'):return None
    return cleaned,{'category':found[0][1] if found else None,
                    'max_difficulty':int(difficulty[1]) if difficulty else None}


def ingredient_entries(raw):
    entries=[];start=0;depth=0
    for index,char in enumerate(raw):
        if char in '（(':depth+=1
        elif char in '）)':depth=max(0,depth-1)
        elif char in '，,、' and depth==0:
            entries.append(raw[start:index].strip());start=index+1
    entries.append(raw[start:].strip())
    return [value for value in entries if value]


def ingredient_fields(rows,original,*,normalize_aliases=True,calculation=False):
    required,optional,tools,uncertain=set(),set(),set(),[]
    evidence=[]
    # A flattened materials list can repeat a group expanded in the quantity table.
    groups=set()
    nonempty=[line for line in original if line.strip()]
    for line,following in zip(nonempty,nonempty[1:]):
        match=re.match(r'^\s*[-*+]\s+(.+)',line)
        if (match and re.match(r'^\s*[-*+]\s+',following)
                and len(following)-len(following.lstrip())>len(line)-len(line.lstrip())):
            groups.add(match[1].strip())
    for row in rows:
        content=row['content'].splitlines()
        starts=[i for i in range(row['source_start_line']-1,row['source_end_line'])
                if original[i:i+len(content)]==content]
        if len(starts)!=1:raise ValueError('原料表与活动知识库不同，请核对来源版本')
        evidence.append({'title':row['title'],'section':row['section'],'source':row['source'],
            'quote':'\n'.join(content),'start_line':starts[0]+1,'end_line':starts[0]+len(content)})
        for index,line in enumerate(content):
            match=re.match(r'^\s*[-*+]\s+(.+)',line)
            if not match:continue
            if match[1].strip() in groups:continue
            # Distributed quantities need the original combined names and amounts.
            if re.search(r'各\s*(?:\d|[一二两三四五六七八九十半]+(?:个|只|块|条|根|把))',match[1]):
                uncertain.append(match[1]);continue
            if calculation and re.match(r'^(?:如果|若|假如|可以加入|可酌情)',match[1]):
                uncertain.append(match[1]);continue
            entries=ingredient_entries(match[1])
            if calculation and '=' in entries[0]:
                # Formula notes after the assigned amount are instructions, not
                # additional ingredients ("鸡蛋 = ...，向上取整").
                entries=entries[:1]
            for raw in entries:
                if calculation and re.match(r'^(?:约\s*\d|(?:切碎|磨碎|去皮切块|掰成小朵)(?:[（(]|$))',raw):continue
                identity=raw
                if calculation:
                    identity=re.sub(r'(?:的)?(?:用量|数量)(?=\s*=)', '',raw)
                    identity=re.sub(r'(?:的)?用量(?:通常来说)?为(?=\s*\d)', ' = ',identity)
                    if re.search(r'共(?:计)?\s*\d',identity):
                        identity=re.sub(r'共(?:计)?(?=\s*\d)','',identity)
                        uncertain.append(match[1])
                    if re.search(r'足够.+的量$',identity):
                        uncertain.append(raw);continue
                name,quantity_detail=ingredient_identity(identity,aliases=normalize_aliases)
                if quantity_detail:uncertain.append(raw+'（数量与规格需核对）')
                counted=COUNTED_FOOD.match(raw)
                if counted and counted['part']:uncertain.append(raw+'（部位和数量需核对）')
                if TOOLS.search(name):tools.add(name)
                elif re.search(r'如果|或者|或|可以不|不用|如下|食材|原料|工具|配料',raw) or not re.fullmatch(r'[\u4e00-\u9fff]{1,12}',name):uncertain.append(raw)
                elif re.search(r'可选|非必[需须]|不必',raw):optional.add(name)
                else:required.add(name)
    return required,optional,tools,uncertain,evidence


def recipe_ingredient_fields(rows,original):
    """Combine listed materials and quantity sections without making options mandatory."""
    tables=[r for r in rows if '必备原料' in r['section']]
    calculations=[r for r in rows if r['section'].split(' > ')[0]=='计算']
    required,optional,tools,uncertain,evidence=ingredient_fields(tables,original)
    extra,extra_optional,extra_tools,extra_uncertain,extra_evidence=ingredient_fields(
        calculations,original,calculation=True)
    if '鸡腿肉' in required and '鸡腿' in extra and '鸡腿' not in required:
        extra.remove('鸡腿')
        extra_uncertain.append('原料表写鸡腿肉，计算章节按鸡腿个数计量，请核对规格和用量')
    extra={name for name in extra if '新鲜'+name not in required|optional}
    optional|=extra_optional
    required=(required|extra)-optional
    return required,optional,tools|extra_tools,list(dict.fromkeys(uncertain+extra_uncertain)),evidence+extra_evidence


def matched_inventory(required,optional,uncertain,stock):
    """Display explicitly named stock, including source alternatives, without resolving their conditions."""
    named=required|optional|{normalize_ingredient(raw) for raw in uncertain}
    return named & set(stock)


@lru_cache(maxsize=4)
def _ingredient_vocabulary(path,stamp,size):
    groups={}
    for line in Path(path).read_text().splitlines():
        row=json.loads(line)
        if '必备原料' in row['section']:groups.setdefault(row['source'],[]).append(row)
    names=set(ALIASES)|set(ALIASES.values())
    for source,rows in groups.items():
        required,optional,_,_,_=ingredient_fields(rows,(ROOT/source).read_text().splitlines())
        names.update(required|optional)
    return frozenset(names)


def ingredient_vocabulary():
    """Recognize names from the same verified ingredient tables used to recommend."""
    from rag_dataset import FOLDER
    path=FOLDER/'chunks.jsonl';stat=path.stat()
    return _ingredient_vocabulary(str(path),stat.st_mtime_ns,stat.st_size)


def select_inventory_recipes(ranked,ingredients,limit):
    """Use the available ingredients across the batch, then favor fewer missing ingredients."""
    stock={normalize_ingredient(s) for s in ingredients}
    uncovered=set(stock);remaining=list(ranked);chosen=[]
    while remaining and len(chosen)<limit:
        def gain(i):
            row=remaining[i]
            matches=set(row.get('inventory_matches',row.get('matched',[])))
            title=normalize_ingredient(row['title'].replace('西红柿','番茄').replace('马铃薯','土豆'))
            title_matches=sum(name in title for name in stock & matches)
            newly_matched=len(uncovered & matches)
            gain=bool(newly_matched) if limit-len(chosen)>=len(uncovered) else newly_matched
            return (gain,len(stock & matches),title_matches,row.get('score',row.get('rerank_score',0)),
                    -len(row.get('missing',[])))
        index=max(range(len(remaining)),key=gain)
        row=remaining.pop(index);chosen.append(row)
        uncovered.difference_update(row.get('inventory_matches',row.get('matched',[])))
    return chosen


def unused_inventory_note(ingredients,rows):
    covered={s for row in rows for s in row.get('inventory_matches',row.get('matched',[]))}
    missing=sorted({normalize_ingredient(s) for s in ingredients}-covered)
    return '本次推荐尚未用到你提供的：'+'、'.join(missing)+'。' if missing else ''


def recommend(ingredients, chunks, limit=3, *, category=None, max_difficulty=None):
    from recipe_catalog import CATEGORIES
    if category is not None and category not in CATEGORIES:raise ValueError('未知菜谱分类')
    if max_difficulty is not None and (type(max_difficulty) is not int or not 1<=max_difficulty<=5):
        raise ValueError('难度上限须为1至5')
    inventory = set(normalize_ingredient(x) for x in ingredients)
    if not inventory or not 1 <= limit <= 10:
        raise ValueError('食材不能为空，推荐数量须为1至10')
    groups = {}
    for row in chunks:
        if '必备原料' in row['section'] or row['section'].split(' > ')[0]=='计算':
            groups.setdefault(row['source'], []).append(row)
    candidates = []
    for source, rows in groups.items():
        if not any('必备原料' in r['section'] for r in rows):continue
        original = (ROOT / source).read_text().splitlines()
        parts=PurePosixPath(source).parts
        source_category=parts[parts.index('dishes')+1] if 'dishes' in parts and len(parts)>parts.index('dishes')+2 else 'other'
        difficulty_rows=[(i,line,re.search(r'预估烹饪难度\s*[：:]\s*(★{1,5})(?!★)',line)) for i,line in enumerate(original,1)]
        difficulty_rows=[(i,line,match) for i,line,match in difficulty_rows if match]
        difficulty=len(difficulty_rows[0][2][1]) if len(difficulty_rows)==1 else 0
        if category is not None and source_category!=category:continue
        if max_difficulty is not None and not 1<=difficulty<=max_difficulty:continue
        required,optional,tools,uncertain,evidence=recipe_ingredient_fields(rows,original)
        matched = required & inventory
        if not required or not matched:
            continue
        optional_matches = optional & inventory
        uncertain_matches = {normalize_ingredient(raw) for raw in uncertain} & inventory
        if max_difficulty is not None:
            number,line,_=difficulty_rows[0]
            evidence.append({'title':rows[0]['title'],'section':'难度','source':source,
                'quote':line,'start_line':number,'end_line':number})
        candidates.append({'title':rows[0]['title'], 'source':source,
            'category':source_category,'difficulty':difficulty,
            'matched':sorted(matched),
            'matched_optional':sorted(optional_matches),'matched_uncertain':sorted(uncertain_matches),
            'reason':'这道菜能用上你手边的'+ '、'.join(sorted(matched))+'。',
            'missing':sorted(required-inventory), 'required':sorted(required),
            'optional':sorted(optional), 'tools':sorted(tools), 'unresolved':uncertain,
            'evidence':evidence})
    ranked=sorted(candidates,key=lambda r:(-len(r['matched']),len(r['missing']),r['difficulty'] or 6,r['source']))
    return select_inventory_recipes(ranked,inventory,limit)


def recommendation_answer(question, chunks):
    parsed=recommendation_filters(question)
    if parsed is None:return None
    cleaned,filters=parsed
    inventory = parse_inventory(cleaned)
    if inventory is None:
        return None
    m = re.search(r'(?:推荐|给我)\s*([1-9一二两三四五六七八九])\s*(?:个|道|种)', question)
    numbers = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    limit = numbers.get(m[1], int(m[1]) if m and m[1].isdigit() else 3) if m else 3
    results = recommend(inventory, chunks, limit, **filters)
    if not results:
        return {'status':'insufficient','short_answer':'知识库未找到可识别原料表与现有食材匹配的菜谱。', 'sources':[], 'evidence':[]}
    texts, evidence, sources = [], [], []
    for row in results:
        ident = len(evidence)+1
        text = f"{row['title']}：已有：{'、'.join(row['matched'])}。"
        if filters['category']:
            from recipe_catalog import CATEGORIES
            text+='原库分类：'+CATEGORIES[row['category']]+'。'
        if filters['max_difficulty'] is not None:text+=f"原文难度：{row['difficulty']}星。"
        if row['missing']:text += '未说明是否有：'+'、'.join(row['missing'])+'。'
        if row['optional']:text += '可选：'+'、'.join(row['optional'])+'。'
        if row['tools']:text += '原文工具：'+'、'.join(row['tools'])+'。'
        if row['unresolved']:text += '待核对条目：'+'；'.join(row['unresolved'])+'。'
        texts.append(text+''.join(f'[{i}]' for i in range(ident,ident+len(row['evidence']))))
        for item in row['evidence']:
            item = {**item, 'reference_id':len(evidence)+1}
            evidence.append(item)
            sources.append({k:item[k] for k in ('reference_id','title','section','source')} | {'line':item['start_line']})
    note = unused_inventory_note(inventory,results)+'已核对原料表和计算章节。未默认拥有油盐等调料。操作中可能另有用料，用量与替代仍需查原文。'
    return {'status':'verified_source', 'short_answer':'\n'.join(texts)+'\n'+note,
        'sources':sources, 'evidence':evidence, 'recommendations':results,
        'filters':filters,'answer_method':'ingredient_table_match', 'validation':note, 'images':[]}
