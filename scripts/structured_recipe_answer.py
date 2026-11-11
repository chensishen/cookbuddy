"""三篇菜谱试验样本的用料比较。"""
import hashlib
import json
import re
from pathlib import Path

from retrieval_candidates import ingredient_candidates, match_titles
from recipe_schema import validate

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "data/structured/recipe_pilot/gongbao_chicken.json"
PILOTS = {'宫保鸡丁': PILOT, '凉拌黄瓜': PILOT.with_name('cucumber_salad.json'),
          '基础牛奶面包': PILOT.with_name('milk_bread.json')}


def compare(recipe, version, inventory):
    plan = next(p for p in recipe['version_preparation'] if p['version'] == version)
    entities = {e['entity_id']: e for e in recipe['entities']}
    groups = {g['group_id']: g for g in recipe['alternative_groups']}
    names = {e['canonical_name']: e for e in recipe['entities']}
    results, seen = [], set()
    for entry in plan['entries']:
        entity = entities[entry['entity_id']]
        gid = entry['alternative_group']
        key = gid or entity['entity_id']
        if key in seen:
            continue
        seen.add(key)
        members = groups[gid]['members'] if gid else [entity['canonical_name']]
        # 只使用本样本明确记录的名称与别名，不推导生熟互换。
        satisfied = [member for member in members
                     if ({member} | set(names[member]['aliases'])) & set(inventory)]
        observations = [o for member in members for o in names[member]['observations']
                        if o['version'] == version]
        evidence = {o['start_line']: o for o in observations}
        results.append({
            'members': members, 'status': 'available' if satisfied else 'missing',
            'matched_members': satisfied,
            'conditions': sorted({o['condition_raw'] for o in observations if o['condition_raw']}),
            'evidence': [evidence[line] for line in sorted(evidence)],
        })
    return results


def select_pilot(question, chunks):
    inventory, _ = ingredient_candidates(question, [])
    if not inventory or not re.search(r'推荐|可以做什么|能做什么|可以考虑|可考虑|还缺', question):
        return None
    if re.search(r'过敏|忌口|不吃|不能吃|步骤|怎么做|几份|多少|复杂|进阶', question):
        return None
    titles = match_titles(question, [c['title'] for c in chunks])
    if len(titles) != 1 or titles[0] not in PILOTS:
        return None
    recipe = json.loads(PILOTS[titles[0]].read_text())
    if not any(c['source'] == recipe['source'] for c in chunks):
        return None
    validate(recipe)
    if not recipe["activation"]["enabled"]:
        return None
    source = ROOT / recipe['source']
    if hashlib.sha256(source.read_bytes()).hexdigest() != recipe['source_sha256']:
        raise ValueError(f"{recipe['title']}结构化样本与原文版本不一致，请先重新核对。")
    if recipe['answer_policy']['status'] not in ('user_confirmed_for_this_sample', 'user_authorized_sample_integration'):
        raise ValueError('结构化样本尚未获得接入授权。')
    # 所有观察逐字核对；草稿重建或原文改变不能悄悄沿用旧依据。
    lines = source.read_text().splitlines()
    for entity in recipe['entities']:
        for obs in entity['observations']:
            if obs['source'] != recipe['source'] or obs['quote'] != '\n'.join(lines[obs['start_line']-1:obs['end_line']]):
                raise ValueError('结构化样本的原文引用不一致。')
    return recipe


def prepare_answer(question, recipe, references):
    inventory, _ = ingredient_candidates(question, [])
    comparison = compare(recipe, '简易版本' if recipe['recipe_id'] == 'gongbao_chicken' else None, inventory)

    def evidence_for(lines):
        refs = []
        for line in sorted(set(lines)):
            row = next((row for row in references if row['source'] == recipe['source']
                        and row['content_start_line'] <= line < row['content_start_line'] + len(row['content'].splitlines())), None)
            if row is None:
                raise ValueError(f"{recipe['title']}原文第 {line} 行缺少完整章节依据。")
            refs.append({'reference_id': row['reference_id'], 'start_line': line, 'end_line': line})
        return refs

    available = [r for r in comparison if r['status'] == 'available']
    missing = [r for r in comparison if r['status'] == 'missing' and r['members'] != ['水']]
    owned = '、'.join(member for r in available for member in r['matched_members']) or '所列库存尚未匹配本版本用料'
    missing_label = '还需补充' if re.search(r'只有|没有其他', question) else '未说明是否有'
    missing_text = '、'.join('或'.join(r['members']) for r in missing)
    if recipe['recipe_id'] == 'milk_bread':
        special = {'水', '植物油', '谷朊粉', '香草精', '黄油', '玉米油'}
        basic = [r for r in comparison if not set(r['members']) & special]
        owned_basic = '、'.join(member for r in basic for member in r['matched_members']) or '尚未匹配基础配料'
        missing_basic = '、'.join('或'.join(r['members']) for r in basic if r['status'] == 'missing')
        text = f'可考虑基础牛奶面包。已匹配：{owned_basic}；面粉替代组任选一种，奶制品允许使用一种或混合使用。'
        text += f'{missing_label}：{missing_basic}。' if missing_basic else '基础配料名称已覆盖。'
        text += '另需准备酵头用的30℃温水；即使已有水，也需要核对温度。这里只核对名称，尚未判断已有数量是否足够。'
        fat = next(r for r in comparison if '黄油' in r['members'])
        fat_status = '已匹配：'+'、'.join(fat['matched_members'])+'。' if fat['matched_members'] else '黄油或玉米油的库存未匹配。'
        condition = fat_status+'油脂能否省略取决于所选奶制品含脂量是否极高；只有“牛奶”这一名称不足以判定，若不满足原文条件则需黄油或玉米油。容器防粘还使用植物油，此用途单列，不自动认定面团油脂可以同时满足。谷朊粉、香草精及表面刷蛋液为可选，不列为必补项。'
        tools = '需另检查工具：可控温烤箱、量杯、布、擀面杖，以及硅油纸或模具；工具不列为食材。原文整形使用垫硅油纸的烤盘或涂油脂的模具，其他工序工具仍需按原文核对。酵头、面团属于制作过程中的中间产物，不作为额外购买食材。'
        return {'claims':[
            {'text':text,'evidence':evidence_for([15,16,17,18,19,20,65,66,67,68,70,71,72,73])},
            {'text':condition,'evidence':evidence_for([21,22,23,74,75,76,133,155])},
            {'text':tools,'evidence':evidence_for([27,28,29,30,31,64,69,119,142])}
        ],'missing_information':''}
    if recipe['recipe_id'] == 'cucumber_salad':
        text = f'可考虑凉拌黄瓜。已匹配：{owned}（本菜谱中的蒜与大蒜按同一食材匹配）。'
        text += f'{missing_label}：{missing_text}。' if missing else '原文用料名称已覆盖。'
        text += '这是按原文基础做法核对食材名称，已有数量和工具尚未检查，不能据此认定可以直接制作。'
        differences = '原料表差异：盐、香油、蚝油在原料表中未列出，但计算和操作明确使用；白糖仅在操作中出现，原文未标可选，本次按原文步骤列入待补或未说明项。冷藏为可选做法，若采用则需另检查冰箱和保鲜膜，不把它们列为食材。'
        return {'claims':[
            {'text':text,'evidence':evidence_for([o['start_line'] for r in comparison for o in r['evidence']])},
            {'text':differences,'evidence':evidence_for([11,12,13,14,26,27,28,35,36,37,42,43])}
        ],'missing_information':''}
    text = f'可考虑宫保鸡丁，未指定版本时，本次按简易版本核对。已匹配：{owned}；替代组满足一种即可，不需要再购买同组另一种。'
    text += f'{missing_label}：{missing_text}。' if missing else '该版本所列食材和调料名称已覆盖。'
    text += '水需另核对，原文涉及清水、开水和热水条件；已有数量和工具尚未检查，不能据此认定可以直接制作。'
    used_lines = [11, 13] + [o['start_line'] for r in comparison for o in r['evidence']]
    differences = '原料表差异：老抽、花椒、香醋、鸡精、芝麻油在计算中归为进阶配料，简易步骤仍使用；生抽在原料表和计算中列出，但两个版本步骤未明确使用，保留待确认。豆瓣酱、莴笋、生花生、油泼辣子在总表标为可选，复杂版本步骤使用，本次不列为简易版本待补项。'
    return {'claims': [
        {'text': text, 'evidence': evidence_for(used_lines)},
        {'text': differences, 'evidence': evidence_for([line for issue in recipe['issues'] for line in issue['lines']])},
    ], 'missing_information': ''}


def validate_structured_answer(answer, question, recipe, references):
    # 与样本重新比较，替代组及版本范围使用此专用校验，不能套用旧的扁平列表校验。
    payload = json.loads(answer)
    expected = prepare_answer(question, recipe, references)
    if [c['text'] for c in payload['claims']] != [c['text'] for c in expected['claims']]:
        raise ValueError('结构化样本回答与版本用料比较不一致。')
    for actual, wanted in zip(payload['claims'], expected['claims']):
        for item in wanted['evidence']:
            if not any(a['reference_id'] == item['reference_id']
                       and a['start_line'] <= item['start_line'] <= a['end_line'] for a in actual['evidence']):
                raise ValueError('结构化样本回答缺少必要原文依据。')


def prepare_inventory_display(question, recipe, references):
    """User-facing sections from the same verified comparison, without audit prose."""
    from inventory_display import inventory_sections
    original=prepare_answer(question,recipe,references)
    inventory=ingredient_candidates(question,[])[0]
    comparison=compare(recipe,'简易版本' if recipe['recipe_id']=='gongbao_chicken' else None,inventory)
    bread=recipe['recipe_id']=='milk_bread'
    special={'水','植物油','谷朊粉','香草精','黄油','玉米油'} if bread else {'水'}
    core=[r for r in comparison if not set(r['members']) & special]
    missing=[r for r in core if r['status']=='missing']
    confirmations=[];declared=[]
    if recipe['recipe_id']=='gongbao_chicken':
        for generic,members,message in (
                ('鸡肉',{'手枪腿','鸡胸脯肉'},'已有鸡肉需确认部位：原文选用鸡腿肉或鸡胸肉。'),
                ('食用油',{'植物油'},'已有食用油需确认种类：配方使用植物油。')):
            pending=[r for r in missing if set(r['members']) & members]
            if generic in inventory and pending:
                missing=[r for r in missing if r not in pending];confirmations.append(message);declared.append(generic)
    owned=[member for r in core for member in r['matched_members']]+declared
    complete_stock=bool(re.search(r'只有|没有其他',question))
    version='简易版本' if recipe['recipe_id']=='gongbao_chicken' else '原文基础做法'
    conclusion=('材料还不齐，暂不能按'+version+'完整制作。' if complete_stock else
                '部分材料尚未说明是否有，暂不能确认能按'+version+'完整制作。') if missing else ('还需确认已有材料的具体种类，暂不能确认可以直接制作。' if confirmations else '所列主要用料名称已覆盖；还需确认用量和工具是否齐全。')
    sections=[('能不能做',conclusion,original['claims'][0]['evidence'])]
    if owned:sections.append(('已有材料','、'.join(owned)+'。',original['claims'][0]['evidence']))
    if missing:
        sections.append(('还缺哪些材料' if complete_stock else '还需确认的材料',
                         '\n'.join('- '+'或'.join(r['members']) for r in missing),original['claims'][0]['evidence']))
    if bread:
        # Preserve the source's conditional fat rule, optional ingredients and
        # temperature rather than flattening them into unconditional purchases.
        sections.append(('另外需要确认','酵头需要30℃温水。已有数量是否足够仍需核对。',original['claims'][0]['evidence']))
        sections.append(('油脂与可选材料',original['claims'][1]['text'],original['claims'][1]['evidence']))
        sections.append(('所需工具',original['claims'][2]['text'],original['claims'][2]['evidence']))
    else:
        note='\n'.join('- '+text for text in confirmations)+ ('\n' if confirmations else '')
        note+='以上按'+version+'核对；已有材料的数量和工具仍需确认。'
        if recipe['recipe_id']=='gongbao_chicken':
            note+='另需准备清水、开水或热水。'
        sections.append(('另外需要确认',note,original['claims'][0]['evidence']))
    return inventory_sections(sections,references,[c['text'] for c in original['claims']])

