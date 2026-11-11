"""Build three source-first structured candidates after model extraction failed."""
import hashlib
import json

from recipe_schema import ROOT, sections, upgrade, validate
from extract_recipe_pilot import boundary_checks

MANIFEST = ROOT / 'data/structured/extraction-pilot-manifest.json'
OUT = ROOT / 'data/structured/repaired_pilot'


class Builder:
    def __init__(self, item):
        self.item = item
        path = ROOT / item['source']
        self.lines = path.read_text().splitlines()
        self.locations = sections(self.lines)
        self.recipe = {'schema_version': '0.2', 'recipe_id': item['recipe_id'],
            'title': item['title'], 'source': item['source'],
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'curation': 'assistant_source_first_draft', 'review_status': 'pending_user_review',
            'entities': [], 'alternative_groups': [], 'issues': [],
            'extraction': {'method': 'manual_curation', 'model': None,
                           'prompt_version': None, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                           'media_mode': 'text_only'},
            'validation': {'structural_status': 'passed',
                           'semantic_status': 'assistant_targeted_source_review',
                           'warnings': ['依据原文建立关键实体候选，尚无用户全字段验收。']},
            'activation': {'enabled': False, 'reason': '原文优先候选，未接入正式推荐或问答'},
            'answer_policy': None, 'version_preparation': []}

    def obs(self, line, requirement='listed_required', quantity=None, stage=None,
            condition=None, purpose=None):
        return {'requirement': requirement, 'quantity_raw': quantity, 'stage': stage,
                'version': None, 'condition_raw': condition, 'purpose': purpose,
                'source': self.recipe['source'], 'section': self.locations[line],
                'start_line': line, 'end_line': line, 'quote': self.lines[line-1]}

    def add(self, name, category, observations, aliases=(), note=''):
        ident = self.recipe['recipe_id'] + ':' + str(len(self.recipe['entities']) + 1).zfill(3)
        self.recipe['entities'].append({'entity_id': ident, 'canonical_name': name,
            'aliases': list(aliases), 'category': category, 'alternative_group': None,
            'alternative_group_ids': [], 'review_note': note,
            'observations': [self.obs(*row) for row in observations]})

    def issue(self, kind, lines, note):
        self.recipe['issues'].append({'kind': kind, 'lines': lines, 'note': note})

    def finish(self):
        upgrade(self.recipe)
        checked = validate(self.recipe)
        failures = boundary_checks(self.recipe, self.item)
        if failures:
            raise ValueError(self.recipe['title'] + '关键边界未通过：' + '；'.join(failures))
        return self.recipe, checked


def airfryer(b):
    b.add('面包片', 'ingredient', [(11,), (18, 'mentioned', '两片'),
        (22, 'step_usage', '两片'), (23, 'step_usage')],
        note='粗粮面包片只是建议，不作为库存必须匹配的别名。')
    b.add('空气炸锅', 'tool', [(12,), (23, 'step_usage'), (24, 'step_usage')])
    b.issue('parameter_scope', [18, 24], '面包片数量为两片；200°C、5分钟属于烘烤参数，不是原料数量。')
    b.issue('nutrition_scope', [29, 31, 32, 33, 34, 35], '附加营养成分不是采购项目或步骤用量。')
    b.issue('bread_suggestion', [11, 22], '粗粮面包片为建议，不把普通面包片判为缺料。')


def tomato_egg(b):
    b.add('西红柿', 'ingredient', [(11,), (24, 'mentioned', '1 个（约 180g） * 份数'),
        (33, 'step_usage'), (38, 'step_usage')])
    b.add('鸡蛋', 'ingredient', [(12,), (25, 'mentioned', '1.5 个 * 份数'),
        (39, 'step_usage'), (44, 'step_usage')])
    b.add('食用油', 'seasoning', [(13,), (26, 'mentioned', '4ml * 鸡蛋/个'),
        (43, 'step_usage')])
    b.add('盐', 'seasoning', [(14,), (27, 'mentioned', '1.5-2g * 份数'),
        (39, 'step_usage', '1g * 份数'), (55, 'step_usage')])
    b.add('糖', 'seasoning', [(15, 'optional'), (28, 'optional', '0-2g * 份数'),
        (55, 'optional', None, None, '如果倾向于甜味版本')])
    b.add('葱花', 'seasoning', [(16, 'optional'), (29, 'optional', '0-10g * 份数'),
        (55, 'optional')])
    b.add('醋', 'seasoning', [(41, 'optional', '1ml', '蛋液准备')])
    b.add('番茄酱', 'seasoning', [(52, 'optional', '10ml', '增汤汁')])
    b.add('水', 'ingredient', [(36, 'conditional', None, '去皮', '可选去皮做法', '烫皮后冷却'),
        (52, 'optional', '50ml', '增汤汁')], aliases=['开水', '冷水', '清水'],
        note='可选去皮用水与可选增汤汁用水分用途记录。')
    b.add('生抽', 'seasoning', [(65, 'optional', '两滴', '附加版本')],
        note='附加版本用两滴生抽替代加盐，不能和基础版盐用量合并。')
    for name, rows in [('碗', [(39, 'step_usage')]), ('锅', [(43, 'step_usage'), (47, 'step_usage')]),
                       ('锅铲', [(49, 'step_usage')]), ('盘', [(45, 'step_usage'), (56, 'step_usage')])]:
        b.add(name, 'tool', rows)
    for name, rows in [('西红柿块', [(38, 'step_usage'), (49, 'step_usage')]),
                       ('鸡蛋液', [(39, 'step_usage'), (44, 'step_usage')]),
                       ('半熟鸡蛋', [(44, 'step_usage'), (45, 'step_usage'), (50, 'step_usage')])]:
        b.add(name, 'intermediate', rows)
    b.issue('formula_scope', [24, 25, 26, 27], '数量依赖份数、鸡蛋向上取整；不能把配方公式当固定采购量。')
    b.issue('optional_scope', [15, 16, 41, 52, 55], '糖、葱花、醋、番茄酱与增汤汁用水均按原文条件记录，不无条件列为缺料。')
    b.issue('version_scope', [14, 55, 65], '附加版本可用两滴生抽代替加盐，和基础做法分开。')


def liangpi(b):
    b.add('凉皮', 'ingredient', [(11,), (18, 'mentioned', '300 g/人'), (49, 'step_usage')])
    b.add('面筋', 'ingredient', [(11,), (30, 'step_usage'), (51, 'step_usage')])
    seasonings = {
        '盐': [(12,), (29, 'step_usage', '1g', '蒜泥'), (35, 'step_usage', '3g', '盐水'), (42, 'step_usage', '4g', '芝麻酱')],
        '鸡精': [(12,), (35, 'step_usage', '2g', '盐水'), (42, 'step_usage', '3g', '芝麻酱')],
        '蚝油': [(12,), (35, 'step_usage', '2g', '盐水'), (42, 'step_usage', '3g', '芝麻酱')],
        '生抽': [(12,), (35, 'step_usage', '5g', '盐水'), (42, 'step_usage', '5g', '芝麻酱')],
        '老抽': [(12,), (35, 'step_usage', '1g', '盐水'), (42, 'step_usage', '1g', '芝麻酱')],
        '香油': [(12,), (35, 'step_usage', '1g', '盐水')],
        '香醋': [(12,), (35, 'step_usage', '5g', '盐水')],
        '芝麻酱': [(12,), (19, 'mentioned', '30 g/人'), (41, 'step_usage'), (42, 'step_usage')],
    }
    for name, rows in seasonings.items():
        b.add(name, 'seasoning', rows)
    b.add('黄瓜', 'ingredient', [(13,), (20, 'mentioned', '100g/人'), (28, 'step_usage'), (53, 'step_usage')])
    b.add('大蒜', 'ingredient', [(13,), (29, 'step_usage', '10g 蒜瓣')], aliases=['蒜瓣'])
    b.add('绿豆芽', 'ingredient', [(13,), (20, 'mentioned', '50g/人'), (27, 'step_usage')])
    for name, rows in [('盆', [(14,), (49, 'step_usage')]),
                       ('碗', [(14,), (35, 'step_usage'), (41, 'step_usage'), (50, 'step_usage')]),
                       ('盘子', [(14,), (27, 'step_usage')]),
                       ('蒜臼', [(14,), (29, 'step_usage')]),
                       ('锅', [(26, 'step_usage'), (27, 'step_usage')]),
                       ('筷子', [(36, 'step_usage'), (43, 'step_usage'), (50, 'step_usage')])]:
        b.add(name, 'tool', rows)
    b.add('水', 'ingredient', [(26, 'step_usage', '500ml', '焯豆芽'),
        (27, 'step_usage', None, '过凉水'), (29, 'step_usage', '10g 自来水', '蒜泥'),
        (30, 'conditional', None, '清洗凉皮', '可用自来水清洗'),
        (36, 'step_usage', '25-35g 温水', '盐水'),
        (44, 'step_usage', '10g 清水', '芝麻酱'),
        (45, 'conditional', None, '芝麻酱', '根据浓稠程度重复 2、3 次')],
        aliases=['凉水', '自来水', '温水', '清水'],
        note='焯煮、清洗、蒜泥、盐水、芝麻酱用途不同；重复加水次数不自动合算。')
    b.add('辣椒油', 'seasoning', [(54, 'optional')])
    b.add('蒜泥', 'intermediate', [(29, 'step_usage')])
    b.add('盐水', 'intermediate', [(35, 'step_usage'), (36, 'step_usage'), (50, 'step_usage')])
    b.issue('quantity_formula_scope', [18, 19, 20, 40], '按人数的用量与向下取整、每人份说明原样保留，不直接换算。')
    b.issue('water_purpose_scope', [26, 27, 29, 30, 36, 44, 45], '不同步骤的水分用途、用量及重复次数分开，不自动加总。')
    b.issue('optional_scope', [54], '辣椒油仅喜爱时可加入，不列为无条件必备。')


def build():
    manifest = {item['recipe_id']: item for item in json.loads(MANIFEST.read_text())['recipes']}
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    for ident, function in [('auto_airfryer_toast', airfryer), ('auto_tomato_egg', tomato_egg),
                            ('auto_liangpi', liangpi)]:
        builder = Builder(manifest[ident])
        function(builder)
        recipe, checked = builder.finish()
        path = OUT / (ident + '.json')
        path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + '\n')
        records.append({'title': recipe['title'], 'path': str(path.relative_to(ROOT)),
                        **checked, 'activated': False})
    return records


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False))
