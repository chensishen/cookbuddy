"""离线测试已整理的宫保鸡丁样本，不调用 API 或正式 RAG。"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'data/structured/recipe_pilot'


from structured_recipe_answer import compare


def main():
    recipe = json.loads((FOLDER / 'gongbao_chicken.json').read_text())
    source = ROOT / recipe['source']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == recipe['source_sha256']
    lines = source.read_text().splitlines()
    inventory = ['鸡胸脯肉', '熟花生']
    version = '简易版本'
    results = compare(recipe, version, inventory)
    available = [r for r in results if r['status'] == 'available']
    missing = [r for r in results if r['status'] == 'missing']
    assert len(available) == 2
    assert any(r['matched_members'] == ['鸡胸脯肉'] for r in available)
    assert not any('手枪腿' in r['members'] for r in missing)
    expected = {'大葱', '干辣椒', '白糖', '料酒', '盐', '淀粉', '植物油',
                '香醋', '鸡精', '芝麻油', '生姜', '老抽酱油', '花椒', '水'}
    assert {r['members'][0] for r in missing} == expected
    assert not {'豆瓣酱', '莴笋', '生花生', '油泼辣子', '生抽酱油'} & expected
    # 对照用例：手枪腿同样满足肉类组，生花生不能直接满足熟花生。
    control = compare(recipe, version, ['手枪腿', '生花生'])
    assert any(r['matched_members'] == ['手枪腿'] for r in control)
    assert any(r['members'] == ['熟花生'] and r['status'] == 'missing' for r in control)
    for result in results + control:
        assert result['evidence']
        for obs in result['evidence']:
            assert obs['quote'] == '\n'.join(lines[obs['start_line']-1:obs['end_line']])
    report = {
        'question': '我有鸡胸脯肉和熟花生，没有其他食材和调料，可以考虑宫保鸡丁吗？请说明还缺什么。',
        'version': version, 'inventory': inventory, 'method': 'offline_structured_comparison',
        'scope': '只检查名称和版本用料；库存数量、工具和实际烹饪条件未判断。水单独提示。',
        'results': results, 'table_differences': recipe['issues'],
        'checks': {'meat_alternative_satisfied': True, 'no_duplicate_meat_purchase': True,
                   'version_scope_checked': True, 'raw_peanuts_not_equated': True,
                   'source_quotes_match': True},
    }
    (FOLDER / 'gongbao-inventory-test.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    missing_names = ['或'.join(r['members']) for r in missing if r['members'] != ['水']]
    md = ['# 宫保鸡丁结构化样本离线测试\n\n',
          f"问题：{report['question']}\n\n所选版本：简易版本（本次测试设定）。\n\n",
          f"## 简短回答\n\n鸡胸脯肉满足肉类替代组，熟花生也已有；不用再买手枪腿。按原文简易版本，还需补充{'、'.join(missing_names)}。另需准备用水，并满足开水/热水等工序条件。\n\n",
          '这里只核对食材名称，尚未判断已有数量是否足够，也未检查工具。豆瓣酱、莴笋、生花生、油泼辣子没有被列为简易版本的待补项。\n\n',
          f"## 原文依据与来源\n\n替代关系依据：[肉类任选一种]({source}:11)、[辣椒任选一种]({source}:13)。版本步骤中的具体处理方式仍按原文执行。\n\n"]
    for result in results:
        label = ' / '.join(result['members'])
        status = '已有：'+'、'.join(result['matched_members']) if result['status']=='available' else ('需准备用水' if label=='水' else '待补')
        md.append(f"### {label}：{status}\n\n")
        for obs in result['evidence']:
            md.append(f"[{obs['section']} · L{obs['start_line']}]({source}:{obs['start_line']})\n\n> {obs['quote']}\n\n")
    md.append('## 原料表差异\n\n')
    for issue in recipe['issues']:
        refs = ' '.join(f"[L{line}]({source}:{line})" for line in issue['lines'])
        md.append(f"- {issue['note']} {refs}\n")
    md.append('\n## 检查结果\n\n肉类替代、避免重复购买、版本范围、生熟花生区分及逐字引用检查通过。该结果来自结构化样本的规则比较，未调用生成模型，也不代表正式 RAG 已修复。\n')
    (FOLDER / 'gongbao-inventory-test.md').write_text(''.join(md))
    print(json.dumps({'available_groups':len(available),'missing_groups':len(missing),
                      'checks':report['checks'],'report':str(FOLDER/'gongbao-inventory-test.md')},ensure_ascii=False))


if __name__ == '__main__':
    main()
