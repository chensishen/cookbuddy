"""Create separate, source-checked assistant revisions of two model drafts."""
from copy import deepcopy
import hashlib
import json

from recipe_schema import ROOT, sections, upgrade, validate

SOURCE = ROOT / 'data/structured/extraction_pilot'
OUT = ROOT / 'data/structured/repaired_pilot'


def observation(recipe, line, requirement, quantity=None, stage=None, condition=None, purpose=None):
    path = ROOT / recipe['source']
    lines = path.read_text().splitlines()
    return {'requirement': requirement, 'quantity_raw': quantity, 'stage': stage,
            'version': None, 'condition_raw': condition, 'purpose': purpose,
            'source': recipe['source'], 'section': sections(lines)[line],
            'start_line': line, 'end_line': line, 'quote': lines[line-1]}


def entity(recipe, name):
    return next(row for row in recipe['entities'] if row['canonical_name'] == name)


def repair_cola(recipe):
    ginger = entity(recipe, '生姜')
    ginger['observations'].append(observation(recipe, 41, 'step_usage', stage='煎制', purpose='剩余姜片爆香'))
    entity(recipe, '小葱')['observations'].append(observation(recipe, 32, 'mentioned', stage='计算', purpose='挽成葱结'))
    entity(recipe, '鸡翅中')['observations'].append(observation(
        recipe, 38, 'conditional', stage='准备', condition='刚买来的鸡翅', purpose='可直接改刀腌制'))
    salt = entity(recipe, '盐')
    unique = {}
    for row in salt['observations']:
        key = (row['start_line'], row['end_line'], row['requirement'], row['quantity_raw'],
               row['stage'], row['version'], row['condition_raw'], row['purpose'])
        unique[key] = row
    salt['observations'] = list(unique.values())
    recipe['issues'] = [
        {'kind': 'quantity_discrepancy', 'lines': [27, 40, 43],
         'note': '生抽计算写15克；操作写约10克腌制及3克调味，不能自动改写任一处。'},
        {'kind': 'optional_scope', 'lines': [28, 43],
         'note': '老抽在计算写3克，操作第5步只说可以适当使用老抽调色；不列为无条件必需。'},
        {'kind': 'step_material_not_in_table', 'lines': [11, 18, 36, 41],
         'note': '冷水与起油出现在操作中，未在必备原料表列出；单列用途，不猜数量。'},
    ]
    return recipe


def repair_steamed_egg(recipe):
    egg = entity(recipe, '新鲜鸡蛋')
    egg['aliases'] = []
    egg['review_note'] = '原料要求新鲜鸡蛋；只有“鸡蛋”的库存表述尚不能确认新鲜程度。'
    egg['observations'].append(observation(recipe, 23, 'step_usage', stage='蛋液准备'))
    water = entity(recipe, '热水')
    water['canonical_name'] = '水'
    water['aliases'] = ['热水', '温水']
    water['review_note'] = '原料/计算写热水；操作写20~30度温水，锅中另有约3cm深水。不同用途不自动合并数量或温度。'
    water['observations'].append(observation(recipe, 24, 'step_usage', quantity='1.5 倍',
        stage='蛋液混合', condition='温度 20~30', purpose='与蛋液混合'))
    water['observations'].append(observation(recipe, 26, 'step_usage', quantity='大约 3cm',
        stage='蒸制', purpose='锅中用水'))
    entity(recipe, '盐')['observations'].append(observation(recipe, 24, 'step_usage',
        stage='蛋液混合', purpose='先在水中化开'))
    entity(recipe, '蛋液')['observations'].append(observation(recipe, 24, 'step_usage', stage='蛋液混合'))
    # “也可加入”是三个各自可选的佐料，不是必须从中选一项的替代组。
    recipe['alternative_groups'] = [group for group in recipe['alternative_groups']
                                     if group['group_id'] != 'optional_seasonings']
    for name in ('蒸鱼豉油', '葱花', '香油'):
        row = entity(recipe, name)
        row['alternative_group'] = None
        row['alternative_group_ids'] = []
    recipe['alternative_groups'].append({
        'group_id': 'cover_in_step', 'members': ['锡纸', '盘子'],
        'satisfaction_rule': 'any_one', 'note': '操作第4步：锡纸包裹，或用盘子盖住。'})
    foil = entity(recipe, '锡纸')
    foil['alternative_group_ids'].append('cover_in_step')
    recipe['entities'].append({
        'entity_id': recipe['recipe_id'] + ':013', 'canonical_name': '盘子', 'aliases': [],
        'category': 'tool', 'alternative_group': 'cover_in_step',
        'alternative_group_ids': ['cover_in_step'], 'review_note': '只在操作中作为盖碗选项。',
        'observations': [observation(recipe, 26, 'step_usage', stage='蒸制', purpose='盖住碗')]})
    recipe['issues'] = [
        {'kind': 'temperature_and_quantity_scope', 'lines': [12, 19, 24],
         'note': '原料与计算写热水、260ml；操作写20~30度温水、蛋液1.5倍。保留两处，不自动认定等价。'},
        {'kind': 'water_purpose_scope', 'lines': [24, 26],
         'note': '蛋液用温水与锅中约3cm深水用途不同，不可相加为同一配方用量。'},
        {'kind': 'cover_alternative_scope', 'lines': [13, 26],
         'note': '原料栏为锡纸或保鲜膜；操作栏为锡纸或盘子，两个来源范围不同。'},
    ]
    return recipe


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for ident, repair in [('auto_cola_wings', repair_cola), ('auto_steamed_egg', repair_steamed_egg)]:
        original_path = SOURCE / ident / 'recipe.json'
        original = json.loads(original_path.read_text())
        recipe = repair(deepcopy(original))
        recipe['curation'] = 'assistant_revision_of_model_draft'
        recipe['review_status'] = 'pending_user_review'
        recipe['extraction'] = {'method': 'manual_curation', 'model': None, 'prompt_version': None,
            'source_sha256': recipe['source_sha256'], 'media_mode': 'text_only',
            'derived_from': str(original_path.relative_to(ROOT)),
            'draft_sha256': hashlib.sha256(original_path.read_bytes()).hexdigest()}
        recipe['validation'] = {'structural_status': 'passed',
                                'semantic_status': 'assistant_targeted_source_review',
                                'warnings': ['仅针对已发现的问题和关键章节修订，非全字段用户验收。']}
        recipe['activation'] = {'enabled': False, 'reason': '独立修订候选，尚未接入正式推荐或问答'}
        recipe['answer_policy'] = None
        recipe['version_preparation'] = []
        upgrade(recipe)
        result = validate(recipe)
        output = OUT / (ident + '.json')
        output.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + '\n')
        results.append({'title': recipe['title'], 'path': str(output.relative_to(ROOT)),
                        **result, 'activated': False})
    summary = {'recipes': results, 'original_drafts_preserved': True,
               'scope': '助手对两篇旧草稿的定点来源修订；未启用正式问答。'}
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False))
