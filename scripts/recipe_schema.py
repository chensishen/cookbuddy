"""菜谱结构化格式 0.2：统一字段及来源检查，不代替语义审核。"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.2'
CATEGORIES = {'ingredient', 'seasoning', 'tool', 'intermediate', 'finished_product'}
REQUIREMENTS = {'listed_required', 'optional', 'conditional', 'advanced_list', 'step_usage', 'mentioned'}
RULES = {'any_one', 'one_or_more', 'conditional_any_one'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_path(relative):
    require(isinstance(relative, str), '原文路径必须是字符串。')
    path = (ROOT / relative).resolve()
    require(path.is_relative_to((ROOT / 'data/HowToCook/dishes').resolve())
            and path.suffix == '.md' and path.is_file(), '原文路径必须指向正式菜谱 Markdown。')
    return path


def sections(lines):
    hierarchy, result = {}, {}
    for number, line in enumerate(lines, 1):
        match = re.match(r'^(#{1,6})\s+(.+)', line)
        if match:
            depth = len(match[1])
            hierarchy = {key: value for key, value in hierarchy.items() if key < depth}
            hierarchy[depth] = match[2]
        result[number] = ' > '.join(value for key, value in sorted(hierarchy.items()) if key > 1)
    return result


def upgrade(recipe, method='manual_curation', model=None, prompt_version=None):
    """追加统一元数据，不改变已有别名、属性、版本与数量解释。"""
    recipe['schema_version'] = VERSION
    recipe['extraction'] = recipe.get('extraction', {
        'method': method, 'model': model, 'prompt_version': prompt_version,
        'source_sha256': recipe['source_sha256'], 'media_mode': 'text_only',
    })
    for entity in recipe['entities']:
        entity.setdefault('alternative_group_ids', [entity['alternative_group']] if entity['alternative_group'] else [])
        for index, obs in enumerate(entity['observations']):
            key = json.dumps([entity['entity_id'], obs['start_line'], obs['end_line'],
                              obs['requirement'], obs['stage'], obs['version'], index], ensure_ascii=False)
            obs['observation_id'] = entity['entity_id'] + ':' + hashlib.sha256(key.encode()).hexdigest()[:12]
    recipe.setdefault('validation', {'structural_status': 'not_checked',
                                    'semantic_status': 'manual_draft', 'warnings': []})
    recipe.setdefault('activation', {
        'enabled': method == 'manual_curation' and 'answer_policy' in recipe,
        'reason': '原有样本基础问答分支' if method == 'manual_curation' else '自动抽取草稿，未接入正式推荐',
    })
    recipe.setdefault('answer_policy', None)
    recipe.setdefault('version_preparation', [])
    return recipe


def validate(recipe):
    require(isinstance(recipe, dict) and recipe.get('schema_version') == VERSION, '结构化版本必须为0.2。')
    for key in ('recipe_id', 'title', 'source_sha256', 'curation', 'review_status'):
        require(isinstance(recipe.get(key), str) and recipe[key], f'{key}必须为非空字符串。')
    require(re.fullmatch(r'[a-z0-9_]+', recipe['recipe_id']) is not None, 'recipe_id格式无效。')
    path = source_path(recipe.get('source'))
    require(hashlib.sha256(path.read_bytes()).hexdigest() == recipe['source_sha256'], '原文哈希不一致。')
    lines = path.read_text().splitlines()
    heading = next((line[2:].strip() for line in lines if line.startswith('# ')), '')
    require(heading.removesuffix('的做法') == recipe['title'], '菜名与原文一级标题不一致。')
    lookup = sections(lines)
    require(isinstance(recipe.get('entities'), list) and recipe['entities'], '食材/工具条目不能为空。')
    names, ids, obs_ids = set(), set(), set()
    for entity in recipe['entities']:
        name, ident = entity.get('canonical_name'), entity.get('entity_id')
        require(isinstance(name, str) and name and name not in names, '条目名称为空或重复。')
        require(isinstance(ident, str) and ident.startswith(recipe['recipe_id']+':') and ident not in ids,
                '条目ID无效或重复。')
        names.add(name); ids.add(ident)
        require(entity.get('category') in CATEGORIES, f'{name}类别无效。')
        aliases = entity.get('aliases')
        require(isinstance(aliases, list) and all(isinstance(a, str) and a for a in aliases)
                and len(set(aliases)) == len(aliases), f'{name}别名无效。')
        require(entity.get('alternative_group') is None or isinstance(entity['alternative_group'], str), '替代组引用无效。')
        require(isinstance(entity.get('alternative_group_ids'),list)
                and len(set(entity['alternative_group_ids']))==len(entity['alternative_group_ids'])
                and all(isinstance(g,str) for g in entity['alternative_group_ids']), '替代组ID列表无效。')
        require(isinstance(entity.get('review_note'), str), '条目说明必须为字符串。')
        require(isinstance(entity.get('observations'), list) and entity['observations'], f'{name}缺少依据。')
        for obs in entity['observations']:
            require(isinstance(obs, dict), '观察格式无效。')
            require(isinstance(obs.get('observation_id'), str) and obs['observation_id'] not in obs_ids, '观察ID无效或重复。')
            obs_ids.add(obs['observation_id'])
            require(obs.get('source') == recipe['source'], '观察引用了其他菜谱。')
            start, end = obs.get('start_line'), obs.get('end_line')
            require(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines), '引用行号越界。')
            require(obs.get('quote') == '\n'.join(lines[start-1:end]) and obs['quote'].strip(), '引用不是逐字原文。')
            require(obs.get('section') == lookup[start], '引用章节不一致。')
            require(obs.get('requirement') in REQUIREMENTS, '观察属性无效。')
            for field in ('quantity_raw', 'stage', 'version', 'condition_raw', 'purpose'):
                require(field in obs, f'缺少统一字段{field}，未知值应明确填null。')
                require(obs.get(field) is None or isinstance(obs[field], str) and obs[field].strip(), f'{field}必须为非空字符串或null。')
            require(obs['requirement'] != 'conditional' or obs['condition_raw'], '有条件属性必须保留条件。')
    require(isinstance(recipe.get('alternative_groups'), list), '替代组必须为列表。')
    groups = {}
    for group in recipe['alternative_groups']:
        gid = group.get('group_id')
        members = group.get('members')
        require(isinstance(gid, str) and gid and gid not in groups, '替代组ID无效或重复。')
        require(isinstance(members, list) and all(isinstance(m, str) for m in members)
                and len(set(members)) >= 2 and len(set(members)) == len(members)
                and set(members) <= names, '替代组成员无效。')
        require(group.get('satisfaction_rule') in RULES and isinstance(group.get('note'), str), '替代规则无效。')
        groups[gid] = group
    for entity in recipe['entities']:
        gid = entity['alternative_group']
        require((gid is None and not entity['alternative_group_ids']) or entity['alternative_group_ids'] and gid==entity['alternative_group_ids'][0], '兼容替代组与完整关系列表不一致。')
        require(all(g in groups and entity['canonical_name'] in groups[g]['members'] for g in entity['alternative_group_ids']), '替代组关系未双向对应。')
    for gid, group in groups.items():
        require(all(any(e['canonical_name'] == member and gid in e['alternative_group_ids']
                        for e in recipe['entities']) for member in group['members']), '组成员缺少反向引用。')
    require(isinstance(recipe.get('issues'), list), '差异必须为列表。')
    for issue in recipe['issues']:
        require(isinstance(issue.get('kind'), str) and isinstance(issue.get('note'), str)
                and isinstance(issue.get('lines'), list) and issue['lines']
                and all(type(line) is int and 1 <= line <= len(lines) for line in issue['lines']), '差异缺少说明或有效依据行。')
    extraction = recipe.get('extraction')
    require(isinstance(extraction, dict) and extraction.get('source_sha256') == recipe['source_sha256']
            and extraction.get('media_mode') == 'text_only'
            and extraction.get('method') in ('manual_curation', 'model_extraction'), '抽取溯源元数据无效。')
    validation = recipe.get('validation')
    require(isinstance(validation, dict) and validation.get('structural_status') in ('not_checked', 'passed')
            and isinstance(validation.get('semantic_status'), str) and isinstance(validation.get('warnings'), list), '校验状态无效。')
    activation = recipe.get('activation')
    require(isinstance(activation, dict) and type(activation.get('enabled')) is bool
            and isinstance(activation.get('reason'), str), '启用状态无效。')
    require(extraction['method'] != 'model_extraction' or not activation['enabled'], '自动抽取草稿不能直接启用正式问答。')
    require(isinstance(recipe.get('version_preparation'), list), '备料方案必须为列表。')
    for plan in recipe['version_preparation']:
        require(plan.get('version') is None or isinstance(plan['version'], str), '版本标记无效。')
        require(isinstance(plan.get('basis'), str) and isinstance(plan.get('entries'), list), '备料方案无效。')
        for entry in plan['entries']:
            entity = next((e for e in recipe['entities'] if e['entity_id'] == entry.get('entity_id')), None)
            require(entity is not None and entry.get('name') == entity['canonical_name']
                    and entity['category'] in ('ingredient', 'seasoning'), '备料方案不能混入工具或中间产物。')
            expected = {obs['start_line'] for obs in entity['observations'] if obs['version'] == plan['version']}
            require(entry.get('alternative_group') == entity['alternative_group']
                    and set(entry.get('evidence_lines', [])) == expected, '备料方案引用不完整或错用版本。')
    return {'entity_count': len(ids), 'observation_count': len(obs_ids),
            'source_hash_matches': True, 'quotes_match': True, 'relationships_valid': True}
