"""核对草稿字段、引用、组成员与此次试验的关键边界；不代表用户批准。"""
import hashlib
import json
from pathlib import Path
from recipe_schema import validate

ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'data/structured/recipe_pilot'


def verify():
    files=[FOLDER/(name+'.json') for name in ('gongbao_chicken','milk_bread','cucumber_salad')]
    recipes=[];observations=0
    for path in files:
        r=json.loads(path.read_text());recipes.append(r)
        validate(r)
        source=ROOT/r['source'];lines=source.read_text().splitlines()
        assert hashlib.sha256(source.read_bytes()).hexdigest()==r['source_sha256'],'源文件已改变'
        assert r['review_status']=='pending_user_review'
        assert len({e['entity_id'] for e in r['entities']})==len(r['entities'])
        names={e['canonical_name'] for e in r['entities']}
        groups={g['group_id']:g for g in r['alternative_groups']}
        for group in groups.values():
            assert len(group['members'])>=2 and set(group['members'])<=names
            assert group['satisfaction_rule'] in ('any_one','one_or_more','conditional_any_one')
            for member in group['members']:
                assert any(e['canonical_name']==member and e['alternative_group']==group['group_id'] for e in r['entities'])
        for e in r['entities']:
            assert e['category'] in ('ingredient','seasoning','tool','intermediate','finished_product')
            assert e['observations']
            assert e['alternative_group'] is None or e['alternative_group'] in groups
            for obs in e['observations']:
                assert obs['source']==r['source']
                assert 1<=obs['start_line']<=obs['end_line']<=len(lines)
                assert obs['quote']=='\n'.join(lines[obs['start_line']-1:obs['end_line']])
                assert obs['requirement'] in ('listed_required','optional','conditional','advanced_list','step_usage','mentioned')
                assert obs['requirement']!='conditional' or obs['condition_raw']
                observations+=1
    chicken,bread,cucumber=recipes
    assert any(g['members']==['手枪腿','鸡胸脯肉'] and g['satisfaction_rule']=='any_one' for g in chicken['alternative_groups'])
    assert any(e['canonical_name']=='豆瓣酱' and any(o['requirement']=='optional' for o in e['observations']) and any(o['version'] for o in e['observations']) for e in chicken['entities'])
    assert chicken['answer_policy']['status']=='user_confirmed_for_this_sample'
    entity_map={e['entity_id']:e for e in chicken['entities']}
    assert len(chicken['version_preparation'])==2
    for plan in chicken['version_preparation']:
        names={entry['name'] for entry in plan['entries']}
        assert {'老抽酱油','花椒','香醋','鸡精','芝麻油','水'}<=names
        assert '生抽酱油' not in names
        assert not {'锅铲','鸡丁','葱姜水','水淀粉'} & names
        for entry in plan['entries']:
            entity=entity_map[entry['entity_id']]
            assert entity['canonical_name']==entry['name']
            assert entity['category'] in ('ingredient','seasoning')
            assert entry['evidence_lines']==sorted({o['start_line'] for o in entity['observations'] if o['version']==plan['version']})
    assert any(issue['kind']=='table_only' for issue in chicken['issues'])
    assert any(e['canonical_name']=='水' and any(o['start_line']==66 and o['stage']=='酵头' for o in e['observations']) for e in bread['entities'])
    assert any(e['canonical_name']=='烤箱' and e['category']=='tool' for e in bread['entities'])
    assert any(e['canonical_name']=='面团' and e['category']=='intermediate' for e in bread['entities'])
    assert any(e['canonical_name']=='黄油' and any(o['requirement']=='conditional' for o in e['observations']) for e in bread['entities'])
    assert any(e['canonical_name']=='蒜' and '大蒜' in e['aliases'] for e in cucumber['entities'])
    assert all(any(e['canonical_name']==name for e in cucumber['entities']) for name in ('盐','香油','蚝油','白糖'))
    report={'recipe_count':3,'entity_count':sum(len(r['entities']) for r in recipes),'observation_count':observations,
            'source_hashes_match':True,'quotes_match':True,'group_references_valid':True,'critical_boundaries_checked':True,
            'review_status':'pending_user_review','limitation':'验证引用和样本边界，不能替代用户确认语义；未验证正式问答效果。'}
    (FOLDER/'validation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':verify()
