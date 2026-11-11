"""补查同一菜谱的计算列表及步骤中的明确用料，保留不确定性。"""
import re


def normalize(text):
    # 原文中这些同一食材的形式不再重复列为新原料；展示仍使用原文名称。
    for old, new in (('姜片','生姜'), ('食用盐','盐'), ('葱结','小葱')):
        text = text.replace(old,new)
    return text


def item_name(raw):
    name = re.split(r'[（(=：:]|\d', raw)[0].strip()
    if not name:
        prefixed=re.match(r'^\d+(?:[.～~-]\d+)?\s*[a-zA-Z克毫升个瓣片根颗盒枚]*\s*([\u4e00-\u9fff]{2,8})',raw)
        if prefixed:name=prefixed.group(1)
    return re.sub(r'(一个|少许|适量)$', '', name)


def crosscheck(prepared, references, inventory, vocabulary):
    claim = prepared['claims'][0]
    table = next(row for row in references if row['reference_id'] == claim['evidence'][0]['reference_id'])
    source = table['source']
    table_text = table['content']
    table_normalized = normalize(table_text)
    extra = []
    mentions = []
    evidence = []
    for row in references:
        if row['source'] != source:
            continue
        for offset, line in enumerate(row['content'].splitlines()):
            number = row['content_start_line'] + offset
            if row['section'].startswith('计算'):
                bullet = re.match(r'^\s*[-*]\s+(.+)', line)
                if not bullet:
                    continue
                raw = bullet.group(1).strip()
                name = item_name(raw)
                if not name or normalize(name) in table_normalized or name in ('每份', '必须配料', '进阶配料', '可选配料'):
                    continue
                # 只收录有数量/公式的计算条目，保留原文字句，不猜别名与替代关系。
                if not re.search(r'\d|=', raw):
                    continue
                state = '已有' if any(term in name for term in inventory) else '待补或待确认'
                extra.append(f'{name}（{state}；计算章节原文：{raw}）')
                evidence.append({'reference_id':row['reference_id'],'start_line':number,'end_line':number})
            elif row['section'].startswith('操作'):
                # 步骤的提及不等于必需，全部标作核对项，尤其保留条件与分支。
                if not re.search(r'加入|倒入|放入|下入|加|放|淋|起油|冷水|清水|热水', line):
                    continue
                names = [name for name in vocabulary if name in line and normalize(name) not in table_normalized]
                names = [name for name in names if not any(name != other and name in other for other in names)]
                if re.search(r'起油|放油|加油|倒油',line) and not any('油' in name for name in names) and '油' not in table_text:
                    names.append('油（原文未说明种类）')
                if names:
                    mentions.extend(names)
                    evidence.append({'reference_id':row['reference_id'],'start_line':number,'end_line':number})
    claim['text'] = claim['text'].replace('这是原料表的逐项比较，尚未核对计算及操作章节是否另有配料。','')
    if extra:
        claim['text'] += '章节配料差异：'+'；'.join(dict.fromkeys(extra))+'。'
    if mentions:
        claim['text'] += '操作章节另提到：'+'、'.join(dict.fromkeys(mentions))+'；请按所引步骤确认用途和条件，不能直接当作全部必需。'
    if not extra and not mentions:
        claim['text'] += '本次规则未发现表外用料；这不代表已穷尽所有配料。'
    claim['text'] += '数量、替代关系与含糊表达仍需核对原文。'
    claim['evidence'].extend(evidence)
    return prepared


def ingredient_vocabulary(chunks):
    words={'冷水','清水','热水','开水','食用油','老抽','生抽'}
    for row in chunks:
        if '必备原料' not in row['section'] and not row['section'].startswith('计算'):
            continue
        for line in row['content'].splitlines():
            m=re.match(r'^\s*[-*]\s+(.+)',line)
            if m:
                name=item_name(m.group(1))
                if re.fullmatch(r'[\u4e00-\u9fff]{2,8}',name) and not re.search(r'锅|刀|盆|碗|机|工具|食材|大约|适量|少许|每份|左右|准备',name):words.add(name)
    return sorted(words,key=lambda word:(-len(word),word))
