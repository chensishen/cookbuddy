"""Run seven isolated name comparisons across all five structured candidates."""
import json

from evaluate_repaired_recipes import compare_inventory
from recipe_schema import ROOT

FOLDER = ROOT / 'data/structured/repaired_pilot'
OUT = ROOT / 'data/evaluation/structured-five-isolated-v1.json'

CASES = [
    ('auto_cola_wings', ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱', '啤酒']),
    ('auto_cola_wings', ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱']),
    ('auto_steamed_egg', ['鸡蛋', '热水', '盐']),
    ('auto_steamed_egg', ['新鲜鸡蛋', '温水', '盐']),
    ('auto_airfryer_toast', ['面包片']),
    ('auto_tomato_egg', ['西红柿', '鸡蛋', '食用油', '盐']),
    ('auto_liangpi', ['凉皮', '面筋', '黄瓜', '大蒜', '绿豆芽']),
]


def evaluate():
    rows = []
    for ident, inventory in CASES:
        recipe = json.loads((FOLDER / (ident + '.json')).read_text())
        rows.append(compare_inventory(recipe, inventory))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'cases': rows, 'enabled_for_live_answer': False,
        'scope': '仅名称、替代组与可选项隔离对照；不判断制作充分性。'}, ensure_ascii=False, indent=2) + '\n')
    return rows


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False))
