"""食材推荐的确定性边界检查，完整语义仍由原文核对完成。"""
import json
import re


def check_unstated_inventory(answer, question):
    """未声明个人库存的问题，不能被生成模型改成已有食材推荐。"""
    if re.search(r'我(?:有|只有|现有)|我的(?:食材|库存)|已有[：:]|现有食材', question):
        return
    for claim in json.loads(answer)['claims']:
        if re.search(r'(?:^|[。；;\n])\s*(?:已有|你有|您有|你拥有|您拥有)(?!菜谱|原文|资料|信息)', claim['text']):
            raise ValueError('用户没有声明已有食材，不能声称个人库存或库存齐全；仍可按用户口味或食材条件推荐菜谱，但不要使用“已有”“你有”等库存表述。')


def check_unsupported_restrictions(answer, evidence):
    """保守拒绝把配料列表或减量建议升级为省略/替代禁令。"""
    by_id = {}
    for item in evidence:
        by_id.setdefault(item['reference_id'], []).append(item['quote'])
    restriction = r'(?:不能|不可以|不可|不允许|不得|禁止|必须)(?:完全)?(?:不放|不加|省略|去掉|删掉|替换|替代)'
    for claim in json.loads(answer)['claims']:
        text = claim['text']
        if not re.search(restriction, text):
            continue
        quotes = '\n'.join(quote for item in claim['evidence']
                           for quote in by_id.get(item['reference_id'], []))
        for clause in re.split(r'[。！？；;\n]', text):
            match = re.search(restriction, clause)
            if not match:
                continue
            prefix = clause[:match.start()]
            if re.search(r'(?:无法判断|不能判断|不能据此|不代表|不足以)[^，,]*$|未说明是否[^，,]*$', prefix):
                continue
            if not re.search(restriction, quotes):
                raise ValueError('原文只列配方或建议，不支持省略或替代禁令；请回答原文已给出的用量，并说明无法据此判断变更是否可行。')


def check_optional_ingredients(answer, references, question):
    if not re.search(r'推荐|可以做什么|能做什么|可以考虑|可考虑', question):
        return
    by_id = {row['reference_id']:row for row in references}
    for claim in json.loads(answer)['claims']:
        text = claim['text']
        optional = set()
        for item in claim['evidence']:
            row = by_id[item['reference_id']]
            if '必备原料' not in row['section']:
                continue
            for line in row['content'].splitlines():
                match = re.match(r'^\s*[-*]\s+([^（(\n]+)[（(]可选[）)]',line)
                if match:
                    optional.add(match.group(1).strip())
        for term in optional:
            if term in text and not re.search(r'可选|非必[需须]|不必|不是必[需须]',text):
                raise ValueError(f'{term} 在原料表中标为可选，不能作为缺少必备原料；请单列可选配料。')


def check_required_ingredients(answer, references, question):
    if not re.search(r'推荐|可以做什么|能做什么|可以考虑|可考虑', question):
        return
    from retrieval_candidates import ingredient_candidates
    inventory, _ = ingredient_candidates(question, [])
    if not inventory:
        return  # 没有可靠的显式清单时不推断拥有关系。
    by_id = {row['reference_id']:row for row in references}
    used_sources = set()
    for claim in json.loads(answer)['claims']:
        tables = [by_id[item['reference_id']] for item in claim['evidence'] if '必备原料' in by_id[item['reference_id']]['section']]
        if not tables:
            continue
        for table in tables:
            used_sources.add(table.get('source',table['title']))
            for line in table['content'].splitlines():
                match = re.match(r'^\s*[-*]\s+(.+)',line)
                if not match or re.search(r'可选|非必[需须]|不必|不用',line):
                    continue
                raw = re.split(r'[（(=：:]|\d',match.group(1))[0].strip()
                raw = re.sub(r'(一个|少许|适量)$','',raw)
                # 组合项逐个检查；“或”的替代项只需提及其中一个。
                for group in raw.split('、'):
                    alternatives = [re.sub(r'也行$','',x).strip() for x in re.split(r'或者|或',group) if x.strip()]
                    if any(term in option for term in inventory for option in alternatives):
                        continue
                    if not any(option in claim['text'] for option in alternatives):
                        raise ValueError(f'未声明拥有的原料 {group} 在原料表中出现，但本项回答遗漏；请写出缺少或未说明的原料。')
    if len(used_sources)>1:
        raise ValueError('本步只推荐一道菜，避免多道菜配料混用。请选择一道并完整比较原料。')


def inventory_recommendation(question, references):
    """明确食材清单的基础推荐：由原料条目比较生成，保留整张原料表。"""
    from retrieval_candidates import ingredient_candidates
    inventory, _ = ingredient_candidates(question, [])
    if not inventory or not re.search(r'推荐|可以做什么|能做什么|可以考虑|可考虑|按原方还需|还需确认|还缺|缺什么',question) or re.search(r'过敏|忌口|不吃|不能吃|不要|辣|清淡|油炸|步骤|怎么做|几份|多少',question):
        return None
    candidates=[]
    for row in references:
        if '必备原料' not in row['section']:
            continue
        entries=[]
        for offset,line in enumerate(row['content'].splitlines()):
            m=re.match(r'^\s*[-*]\s+(.+)',line)
            if m:entries.append((m.group(1).strip(),row['content_start_line']+offset))
        matched={term for term in inventory if any(term in raw and not re.search(r'可选|非必[需须]|不必|如果|不用',raw) for raw,_ in entries)}
        if entries and matched:candidates.append((len(matched),-len(entries),row,entries))
    if not candidates:return None
    _,_,row,entries=max(candidates,key=lambda value:(value[0],value[1]))
    missing=[];optional=[];conditional=[];tools=[]
    for raw,_ in entries:
        if re.search(r'可选|非必[需须]|不必',raw):optional.append(raw);continue
        if re.search(r'如果|不用|可以不',raw):conditional.append(raw);continue
        # 泛称“豆腐”不能直接证明已有原文要求的“内脂豆腐”。
        name=re.split(r'[（(]',raw,1)[0].strip()
        if name in inventory:continue
        if re.search(r'锅|碗|盆|刀|筛|烤箱|空气炸锅|搅拌机|厨房秤|温度计|纱布',raw):tools.append(raw)
        else:missing.append(raw)
    only=bool(re.search(r'只有|没有其他',question))
    text='已有：'+'、'.join(inventory)+'。可考虑'+row['title']+'。'
    text+='按这张必备原料表，'+('还缺：' if only else '未说明是否有：')+'、'.join(missing)+'。' if missing else '按这张必备原料表，所列必备食材已覆盖；用量仍需核对原文。'
    if optional:text+='可选配料：'+'；'.join(optional)+'。'
    if conditional:text+='需按原文条件判断：'+'；'.join(conditional)+'。'
    if tools:text+='未说明的工具：'+'；'.join(tools)+'。'
    text+='这是原料表的逐项比较，尚未核对计算及操作章节是否另有配料。'
    return {'claims':[{'text':text,'evidence':[{'reference_id':row['reference_id'],
        'start_line':entries[0][1],'end_line':entries[-1][1]}]}],'missing_information':''}


def ingredient_list_answer(question, references):
    """仅对明确菜名的原文调料查询，区分调味料、辛香配料与主料。"""
    if not re.search(r'原文.{0,12}(?:哪些|什么)调料|原文需要哪些调料',question):
        return None
    tables=[r for r in references if '必备原料' in r['section']]
    if len(tables)!=1:return None
    table=tables[0]
    if table['title'] not in question:return None
    seasoning={'盐','食盐','生抽','老抽','酱油','陈醋','醋','白糖','糖','蚝油','料酒','胡椒粉','香油','食用油','油'}
    aromatics={'大蒜','蒜','葱','小葱','干辣椒','青椒','红椒','生姜','姜','小米椒'}
    mains={'土豆','鸡蛋','西红柿','鸡翅','豆腐','黄瓜','米饭','冷饭'}
    groups={'调味料':[],'其他配料':[],'主料':[]}
    bullets=[]
    for offset,line in enumerate(table['content'].splitlines()):
        m=re.match(r'^\s*[-*]\s+(.+)',line)
        if not m:continue
        raw=m.group(1).strip();name=re.split(r'[（(]',raw,1)[0].strip()
        category=next((key for key,vocab in [('调味料',seasoning),('其他配料',aromatics),('主料',mains)] if name in vocab),None)
        if not category:return None  # 未知条目不靠词表猜类别。
        groups[category].append(raw);bullets.append(table['content_start_line']+offset)
    if not bullets or not groups['调味料']:return None
    evidence=[{'reference_id':table['reference_id'],'start_line':bullets[0],'end_line':bullets[-1]}]
    calc=[r for r in references if r['source']==table['source'] and r['section'].startswith('计算')]
    extras=[]
    for row in calc:
        for offset,line in enumerate(row['content'].splitlines()):
            m=re.match(r'^\s*[-*]\s+(.+)',line)
            if not m:continue
            raw=m.group(1).strip();name=re.split(r'\s|\d|[（(]',raw,1)[0]
            if name in seasoning and name not in groups['调味料']:
                if not any(raw.startswith(existing) for existing in groups['调味料']):
                    extras.append(raw)
                    n=row['content_start_line']+offset
                    evidence.append({'reference_id':row['reference_id'],'start_line':n,'end_line':n})
    text='必备原料表中的调味料：'+'、'.join(groups['调味料'])+'。'
    if extras:text+='计算章节还列出：'+'、'.join(extras)+'；原料表未列出，需核对两处差异。'
    if groups['其他配料']:text+='葱、蒜、辣椒等另列为其他配料：'+'、'.join(groups['其他配料'])+'。'
    if groups['主料']:text+='主料：'+'、'.join(groups['主料'])+'。'
    return {'claims':[{'text':text,'evidence':evidence}],'missing_information':''}
