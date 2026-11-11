"""Isolated name-level inventory preview for repaired recipes; no live answer integration."""
import hashlib
import json

from recipe_schema import ROOT, validate
from extract_recipe_pilot import boundary_checks

SOURCE = ROOT / 'data/structured/repaired_pilot'
OUT = ROOT / 'data/evaluation/structured-repaired-isolated-v1.json'


def compare_inventory(recipe, inventory):
    validate(recipe)
    lineage = recipe['extraction']
    if 'derived_from' in lineage:
        original = (ROOT / lineage['derived_from']).resolve()
        if (not original.is_relative_to((ROOT / 'data/structured/extraction_pilot').resolve())
                or not original.is_file()
                or hashlib.sha256(original.read_bytes()).hexdigest() != lineage['draft_sha256']):
            raise ValueError('修订候选的原模型草稿来源已变化')
    elif recipe['curation'] != 'assistant_source_first_draft':
        raise ValueError('未知的结构化候选来源')
    manifest = json.loads((ROOT / 'data/structured/extraction-pilot-manifest.json').read_text())
    item = next((row for row in manifest['recipes'] if row['recipe_id'] == recipe['recipe_id']
                 and row['source'] == recipe['source']), None)
    if item is None or boundary_checks(recipe, item):
        raise ValueError('结构化候选与当前关键边界清单不一致')
    if recipe['activation']['enabled']:
        raise ValueError('隔离对照仅接受未启用的修订候选')
    if not isinstance(inventory, list) or any(not isinstance(name, str) or not name for name in inventory):
        raise ValueError('库存必须是明确名称列表')
    owned = set(inventory)
    entities = {e['canonical_name']: e for e in recipe['entities']}
    materials = {name: e for name, e in entities.items() if e['category'] in ('ingredient', 'seasoning')}
    groups = []
    grouped = set()
    for group in recipe['alternative_groups']:
        members = group['members']
        if not all(name in materials for name in members):
            continue
        if not any(obs['section'].startswith('必备原料和工具')
                   for name in members for obs in materials[name]['observations']):
            continue
        matched = [name for name in members
                   if ({name} | set(materials[name]['aliases'])) & owned]
        groups.append({'members': members, 'status': 'named' if matched else 'not_listed',
                       'matched': matched, 'source_lines': sorted({obs['start_line']
                       for name in members for obs in materials[name]['observations']
                       if obs['section'].startswith('必备原料和工具')})})
        grouped.update(members)
    names = []
    for name, entity in materials.items():
        if name in grouped or not any(o['requirement'] == 'listed_required' for o in entity['observations']):
            continue
        matched = bool(({name} | set(entity['aliases'])) & owned)
        names.append({'name': name, 'status': 'named' if matched else 'not_listed',
                      'source_lines': sorted({o['start_line'] for o in entity['observations']
                                              if o['requirement'] == 'listed_required'})})
    step_only = [name for name, entity in materials.items()
                 if name not in grouped and any(o['requirement'] == 'step_usage' for o in entity['observations'])
                 and not any(o['requirement'] in ('listed_required', 'optional') for o in entity['observations'])]
    optional = [name for name, entity in materials.items() if name not in grouped
                and any(o['requirement'] == 'optional' for o in entity['observations'])
                and not any(o['requirement'] == 'listed_required' for o in entity['observations'])]
    tools = [name for name, entity in entities.items() if entity['category'] == 'tool']
    return {'title': recipe['title'], 'inventory': inventory, 'required_names': names,
            'material_alternative_groups': groups, 'step_only_materials': step_only,
            'optional_materials': optional, 'tools_not_assessed': tools,
            'source_issues': recipe['issues'], 'readiness': 'not_assessed',
            'notice': '只对照名称；数量、温度、工具和章节差异未判定，不能据此声称可以直接制作。'}


def evaluate():
    cases = [
        ('auto_cola_wings', ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱', '啤酒']),
        ('auto_cola_wings', ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱']),
        ('auto_steamed_egg', ['鸡蛋', '热水', '盐']),
        ('auto_steamed_egg', ['新鲜鸡蛋', '温水', '盐']),
    ]
    results = []
    for ident, inventory in cases:
        recipe = json.loads((SOURCE / (ident + '.json')).read_text())
        results.append(compare_inventory(recipe, inventory))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'cases': results, 'enabled_for_live_answer': False}, ensure_ascii=False, indent=2) + '\n')
    return results


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False))
