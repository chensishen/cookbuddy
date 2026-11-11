"""Offline recheck of existing model drafts against current source and boundary manifest."""
from datetime import datetime, timezone
import hashlib
import json

from extract_recipe_pilot import boundary_checks
from recipe_schema import ROOT, source_path, validate

MANIFEST = ROOT / 'data/structured/extraction-pilot-manifest.json'
OUT = ROOT / 'data/structured/extraction_pilot'

REVIEW_CARDS = [
    {'recipe_id': 'auto_cola_wings', 'lines': [27, 40, 43],
     'finding': '计算给生抽15克，操作分别写约10克和3克；数量不能直接视为完全一致。'},
    {'recipe_id': 'auto_cola_wings', 'lines': [16, 30, 36, 41],
     'finding': '计算给姜2片，操作先用1片再用剩余姜片；草稿把这说成数量冲突缺少依据，且把2片错归到原料表。'},
    {'recipe_id': 'auto_steamed_egg', 'lines': [12, 19, 24],
     'finding': '原料和计算写热水、260ml；操作写20~30度温水及蛋液1.5倍，须保留章节差异。'},
    {'recipe_id': 'auto_steamed_egg', 'lines': [13, 26],
     'finding': '锡纸或保鲜膜是原料栏替代；操作另提盘子盖碗，不能遗漏或当成食材。'},
    {'recipe_id': 'auto_steamed_egg', 'lines': [24, 25, 26],
     'finding': '原文明确写蛋液/鸡蛋液，复核模型称原文未明确提及蛋液的意见不成立。'},
    {'recipe_id': 'auto_steamed_egg', 'lines': [32],
     'finding': '原文称蒸鱼豉油、葱花香油为佐料；复核模型仅因未逐字写调味料而判类别错误，依据不足。'},
]


def audit():
    items = json.loads(MANIFEST.read_text())['recipes']
    by_id = {item['recipe_id']: item for item in items}
    results = []
    for item in items:
        path = OUT / item['recipe_id'] / 'recipe.json'
        if not path.exists():
            results.append({'recipe_id': item['recipe_id'], 'title': item['title'],
                            'state': 'no_current_draft', 'boundary_failures': []})
            continue
        recipe = json.loads(path.read_text())
        if recipe['source'] != item['source'] or recipe['recipe_id'] != item['recipe_id']:
            raise ValueError('草稿与清单身份不一致：' + item['recipe_id'])
        validation = validate(recipe)
        if recipe['activation']['enabled']:
            raise ValueError('自动抽取草稿被意外启用：' + item['recipe_id'])
        failures = boundary_checks(recipe, item)
        results.append({'recipe_id': item['recipe_id'], 'title': item['title'],
                        'state': 'boundary_pass_semantic_pending' if not failures else 'needs_repair',
                        'entity_count': validation['entity_count'],
                        'observation_count': validation['observation_count'],
                        'boundary_failures': failures, 'activated': False})
    cards = []
    for card in REVIEW_CARDS:
        item = by_id[card['recipe_id']]
        path = source_path(item['source'])
        lines = path.read_text().splitlines()
        if not all(1 <= line <= len(lines) for line in card['lines']):
            raise ValueError('人工核对卡的原文行号越界')
        cards.append({**card, 'title': item['title'], 'source': item['source'],
                      'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'quotes': [{'line': number, 'text': lines[number-1]} for number in card['lines']],
                      'reviewer': 'assistant_source_reading', 'user_reviewed': False})
    summary = {'created_at': datetime.now(timezone.utc).isoformat(),
               'recipe_count': len(items), 'current_drafts': sum(r['state'] != 'no_current_draft' for r in results),
               'boundary_pass_semantic_pending': sum(r['state'] == 'boundary_pass_semantic_pending' for r in results),
               'needs_repair': sum(r['state'] == 'needs_repair' for r in results),
               'no_current_draft': sum(r['state'] == 'no_current_draft' for r in results),
               'activated': 0,
               'limitation': '来源、结构和预设边界重评；人工核对卡由助手读原文整理，未获用户复核，也非全字段准确率。'}
    output = {'summary': summary, 'results': results, 'review_cards': cards}
    (OUT / 'offline-audit.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    return output


if __name__ == '__main__':
    print(json.dumps(audit()['summary'], ensure_ascii=False))
