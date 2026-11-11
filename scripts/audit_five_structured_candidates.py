"""Check the five disabled structured candidates as one reproducible batch."""
import hashlib
import json

from extract_recipe_pilot import boundary_checks
from recipe_schema import ROOT, validate

FOLDER = ROOT / 'data/structured/repaired_pilot'
MANIFEST = ROOT / 'data/structured/extraction-pilot-manifest.json'
EVALUATION = ROOT / 'data/evaluation/structured-five-isolated-v1.json'


def audit():
    items = json.loads(MANIFEST.read_text())['recipes']
    results = []
    for item in items:
        path = FOLDER / (item['recipe_id'] + '.json')
        recipe = json.loads(path.read_text())
        if (recipe['recipe_id'] != item['recipe_id'] or recipe['source'] != item['source']
                or recipe['activation']['enabled'] or recipe['answer_policy'] is not None):
            raise ValueError('候选身份或启用状态不符合隔离要求')
        checked = validate(recipe)
        failures = boundary_checks(recipe, item)
        if failures:
            raise ValueError(item['title'] + '关键边界未通过：' + '；'.join(failures))
        origin = 'source_first' if recipe['curation'] == 'assistant_source_first_draft' else 'repaired_model_draft'
        if origin == 'repaired_model_draft':
            lineage = recipe['extraction']
            original = (ROOT / lineage['derived_from']).resolve()
            if (not original.is_relative_to((ROOT / 'data/structured/extraction_pilot').resolve())
                    or hashlib.sha256(original.read_bytes()).hexdigest() != lineage['draft_sha256']):
                raise ValueError('原模型草稿来源已变化')
        results.append({'title': recipe['title'], 'recipe_id': recipe['recipe_id'],
                        'origin': origin, 'path': str(path.relative_to(ROOT)),
                        'entity_count': checked['entity_count'],
                        'observation_count': checked['observation_count'],
                        'source_hash_matches': checked['source_hash_matches'],
                        'quotes_match': checked['quotes_match'], 'boundary_failures': failures,
                        'activated': False})
    evaluation = json.loads(EVALUATION.read_text())
    if (evaluation['enabled_for_live_answer'] or len(evaluation['cases']) != 7
            or any(case['readiness'] != 'not_assessed' for case in evaluation['cases'])):
        raise ValueError('隔离库存对照被当成正式可制作判断')
    summary = {'candidate_count': len(results),
               'repaired_model_drafts': sum(r['origin']=='repaired_model_draft' for r in results),
               'source_first_candidates': sum(r['origin']=='source_first' for r in results),
               'entity_count': sum(r['entity_count'] for r in results),
               'observation_count': sum(r['observation_count'] for r in results),
               'isolated_inventory_cases': len(evaluation['cases']),
               'ready_for_activation': False,
               'limitation': '结构、来源和预设边界检查通过；尚无用户全字段语义验收或正式问答回归。'}
    output = {'summary': summary, 'results': results}
    (FOLDER / 'five-candidate-audit.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    return output


if __name__ == '__main__':
    print(json.dumps(audit()['summary'], ensure_ascii=False))
