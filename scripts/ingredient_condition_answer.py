"""有限、来源绑定的食材条件判断；不把未知库存或步骤用料猜成可选。"""
import hashlib
import json
import re

from check_embedding import ROOT
from retrieval_candidates import match_titles

POLICIES = ROOT / 'data/structured/ingredient-policies-v1.json'
LIMITATION = '这里只核对原文用料名称和明确条件；份数、库存数量与完整工具是否齐全仍需确认。'


def inventory_conditions(question):
    """只接受短的中文名称列表，数量、转折和复杂否定不猜测。"""
    if re.search(r'除了|都没有|任何|没有其他|不含|不吃|不能吃|'
                 r'不是没有|并非没有|如果|假如|假设|可能|也许|不确定|不知道|未必', question):
        return None
    def terms(raw):
        raw = re.split(r'能否|能不能|能|可以|是否|需要|需不需|要不要|是不是|它们|推荐|按|照', raw)[0].strip()
        values = [s.strip() for s in re.split(r'、|和|及|与|,', raw) if s.strip()]
        if not values or any(not re.fullmatch(r'[\u4e00-\u9fff]{1,10}', s) for s in values):
            return None
        if any(re.search(r'没有|除了|但是|不过|其他|不含|或|都|任何', s) for s in values):
            return None
        return list(dict.fromkeys(values))
    owned_match = re.search(r'我(?:现在)?(只有|有)\s*([^，。；！？?\n]+)', question)
    owned = terms(owned_match.group(2)) if owned_match else []
    absent = []
    for match in re.finditer(r'(?:没有|缺少|没买|缺)\s*([^，。；！？?\n]+)', question):
        values = terms(match.group(1))
        if values is None: return None
        absent.extend(values)
    if owned is None or not owned and not absent: return None
    return {'owned': owned, 'absent': list(dict.fromkeys(absent)),
            'only': bool(owned_match and owned_match.group(1) == '只有')}


def checked_policy(title, chunks):
    policy = next((p for p in json.loads(POLICIES.read_text())['policies'] if p['title'] == title), None)
    if policy is None: return None
    if {r['source'] for r in chunks if r['title'] == title} != {policy['source']}: return None
    source = (ROOT / policy['source']).resolve()
    if not source.is_relative_to((ROOT / 'data/HowToCook/dishes').resolve()):
        raise ValueError('条件规则的来源路径越界')
    if hashlib.sha256(source.read_bytes()).hexdigest() != policy['source_sha256']:
        raise ValueError('食材条件规则与原文版本不同，请重新核对规则')
    lines = source.read_text().splitlines()
    evidence = [e for item in policy['entries'] for e in item['evidence']]
    evidence += policy.get('no_added_oil_in_original', {}).get('evidence', [])
    if any(not 1 <= e['line'] <= len(lines) or lines[e['line']-1] != e['quote'] for e in evidence):
        raise ValueError('食材条件规则的逐字依据不匹配')
    return policy


def select_condition_plan(question, chunks):
    chunks = list(chunks)
    if re.search(r'过敏|忌口|不吃|不能吃|不含|替代|替换|代替|少放|几份|多少|\d|怎么做|做法|步骤|温度|分钟|先|后', question):
        return None
    if not re.search(r'能|可以|还缺|补买|需要|必需|必须|可选|原方|配方|只在', question): return None
    titles = match_titles(question, {r['title'] for r in chunks})
    if len(titles) != 1: return None
    conditions = inventory_conditions(question)
    if conditions is None: return None
    policy = checked_policy(titles[0], chunks)
    if policy is None: return None
    if policy['version'] and '简易' not in question: return None
    entries = policy['entries']
    aliases = {name: item['name'] for item in entries for name in [item['name']] + item['aliases']}
    extra_oil = policy.get('no_added_oil_in_original') and '油' in conditions['absent']
    if any(term not in aliases and not (term == '油' and extra_oil)
           for term in conditions['owned'] + conditions['absent']): return None
    owned = {aliases[t] for t in conditions['owned']}
    absent = {aliases.get(t, t) for t in conditions['absent']}
    if owned & absent: return None
    if not policy['inventory_comparison_enabled'] and owned and not owned <= {
            e['name'] for e in entries if e['role'] == 'avoid'}: return None
    claims = [];display=[]
    def add(text, items, heading='能不能做', display_text=None):
        index=len(claims)
        claims.append({'text': text, 'lines': sorted({e['line'] for item in items for e in item['evidence']})})
        if heading:display.append((index,heading,display_text or text.replace(LIMITATION,'')))
        return index
    avoided = [e for e in entries if e['role'] == 'avoid' and e['name'] in owned]
    if avoided:
        add('不能按原方把' + '、'.join(e['name'] for e in avoided) +
            '视为符合要求的面粉：原文要求高筋或中筋，并明确避免低筋面粉。' + LIMITATION, avoided)
    elif policy['inventory_comparison_enabled'] and owned:
        ingredients = [e for e in entries if e['role'] in ('required', 'step_usage')]
        missing = [e for e in ingredients if e['name'] in absent or conditions['only'] and e['name'] not in owned]
        unknown = [e for e in ingredients if e['name'] not in owned and e not in missing]
        text = '按' + policy['title'] + '原方核对用料名称。'
        if missing: text += '还缺：' + '、'.join(e['name'] for e in missing) + '；不能完整照原方制作。'
        if unknown: text += '未说明是否有：' + '、'.join(e['name'] for e in unknown) + '；需要先确认。'
        if not missing and not unknown: text += '所列食材名称已覆盖，不能据此确认数量与工具也足够。'
        optional = [e for e in entries if e['role'] == 'optional']
        if optional: text += '、'.join(e['name'] for e in optional) + '在原文标为可选，不列为必补项。'
        text += LIMITATION
        index=add(text, ingredients + optional,heading=None)
        conclusion=('材料还不齐，不能完整照原方制作。' if missing else
                    '还有材料未确认是否拥有，暂不能确认能完整照原方制作。' if unknown else
                    '原方所列食材名称已覆盖；还需确认用量与工具。')
        display.append((index,'能不能做',conclusion))
        matched=[e['name'] for e in ingredients if e['name'] in owned]
        if matched:display.append((index,'已有材料','、'.join(matched)+'。'))
        if missing:display.append((index,'还缺哪些材料','\n'.join('- '+e['name'] for e in missing)))
        if unknown:display.append((index,'还需确认的材料','\n'.join('- '+e['name'] for e in unknown)))
        if optional:display.append((index,'可选材料','、'.join(e['name'] for e in optional)+'为可选，不列为必补项。'))
        tools = [e for e in entries if e['role'] == 'required_tool']
        if tools:
            text = '原方要求工具：' + '、'.join(e['name'] for e in tools) + '。'
            if any(e['name'] in absent for e in tools): text += '你明确没有该工具，不能直接照原方制作；原文没有提供其他设备的等效做法。'
            elif all(e['name'] in owned for e in tools): text += '该工具已明确拥有；具体用量仍需核对。'
            else: text += '是否拥有仍需确认；“只有这些食材”不自动表示没有工具。'
            add(text, tools,heading='所需工具')
    else:
        requested = [e for e in entries if e['name'] in absent]
        if not requested: return None
        for role in ('optional', 'required', 'required_tool', 'step_usage', 'avoid'):
            items = [e for e in requested if e['role'] == role]
            if not items: continue
            names = '、'.join(e['name'] for e in items)
            if role == 'optional': text = names + '明确标为可选，不需要仅因缺少它们而补买；其他原料与工具尚未确认。'
            elif role == 'step_usage': text = names + '在' + (policy['version'] or '原方') + '步骤中明确使用，原文未标为可选；缺少时不能认定完整照原方制作，也不能说只在其他版本需要。'
            elif role == 'required_tool': text = names + '是原方列出的必要工具；你明确没有，不能直接照原方制作。'
            elif role == 'required': text = names + '是原方用料，已明确缺少，不能完整照原方制作。'
            else: text = '原文明确避免' + names + '，不据此提供替代方法。'
            heading={'optional':'可选材料','step_usage':'需要补充的材料','required_tool':'所需工具',
                     'required':'需要补充的材料','avoid':'材料要求'}[role]
            if role=='step_usage':
                visible=names+'在'+(policy['version'] or '原方')+'步骤中使用，原文未标为可选，不能认定可以直接省略。'
            else:visible=text
            add(text + LIMITATION, items,heading=heading,display_text=visible)
    if extra_oil:
        add('按这份完整原料表与操作，空气炸锅面包片未要求加油；没有油不构成该原方的缺项。这仅限当前原方，不是对其他做法的判断。',
            [policy['no_added_oil_in_original']],heading='用油要求',
            display_text='这份原方未要求加油，没有油不构成该原方的缺项。')
    if not claims: return None
    return {'source': policy['source'], 'source_sha256': policy['source_sha256'],
            'title': policy['title'], 'claims': claims, 'conditions': conditions,'display_sections':display}


def prepare_condition_answer(plan, references):
    claims = []
    for claim in plan['claims']:
        evidence = []
        for line in claim['lines']:
            row = next((r for r in references if r['source'] == plan['source'] and
                        r['content_start_line'] <= line < r['content_start_line'] + len(r['content'].splitlines())), None)
            if row is None: raise ValueError('食材条件判断缺少对应原文章节')
            evidence.append({'reference_id': row['reference_id'], 'start_line': line, 'end_line': line})
        claims.append({'text': claim['text'], 'evidence': evidence})
    return {'claims': claims, 'missing_information': ''}


def validate_condition_answer(answer, plan, references):
    if json.loads(answer) != prepare_condition_answer(plan, references):
        raise ValueError('食材条件回答与来源规则不一致')


def prepare_condition_display(plan,references):
    from inventory_display import inventory_sections
    original=prepare_condition_answer(plan,references)
    sections=[(heading,text,original['claims'][index]['evidence'])
              for index,heading,text in plan['display_sections']]
    all_evidence=[e for claim in original['claims'] for e in claim['evidence']]
    sections.append(('另外需要确认','份数、已有材料的数量与完整工具是否齐全仍需确认。',all_evidence))
    return inventory_sections(sections,references,[c['text'] for c in original['claims']])
