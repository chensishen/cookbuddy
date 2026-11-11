"""人工整理的三篇结构化样本，按真实行号构建引用；不接入线上推荐。"""
import hashlib
import json
import re
from pathlib import Path

from recipe_schema import upgrade, validate

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/structured/recipe_pilot'


def recipe(title, relative):
    path=ROOT/relative
    lines=path.read_text(encoding='utf-8').splitlines()
    hierarchy={};sections={}
    for number,line in enumerate(lines,1):
        match=re.match(r'^(#{1,6})\s+(.+)',line)
        if match:
            depth=len(match[1]);hierarchy={k:v for k,v in hierarchy.items() if k<depth};hierarchy[depth]=match[2]
        sections[number]=' > '.join(v for k,v in hierarchy.items() if k>1)
    return {'schema_version':'0.1','recipe_id':{'宫保鸡丁':'gongbao_chicken','基础牛奶面包':'milk_bread','凉拌黄瓜':'cucumber_salad'}[title],
            'title':title,'source':relative,'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'curation':'assistant_manual_draft','review_status':'pending_user_review',
            'entities':[],'alternative_groups':[],'issues':[],
            '_lines':lines,'_sections':sections}


def add(r,name,category,observations,aliases=(),group=None,note=''):
    entity={'entity_id':f"{r['recipe_id']}:{len(r['entities'])+1:03d}",'canonical_name':name,'aliases':list(aliases),
            'category':category,'alternative_group':group,'observations':[], 'review_note':note}
    for obs in observations:
        # 行号、属性、数量原文、阶段、版本、条件、用途；属性属于该次观察，不全局强行合并。
        line,attribute,quantity,stage,version,condition,purpose=obs
        entity['observations'].append({'requirement':attribute,'quantity_raw':quantity or None,
            'stage':stage or None,'version':version or None,'condition_raw':condition or None,
            'purpose':purpose or None,'source':r['source'],'section':r['_sections'][line],
            'start_line':line,'end_line':line,'quote':r['_lines'][line-1]})
    r['entities'].append(entity)
    return entity['entity_id']


def o(line,attribute='listed_required',quantity='',stage='',version='',condition='',purpose=''):
    return (line,attribute,quantity,stage,version,condition,purpose)


def group(r,ident,names,rule,note=''):
    r['alternative_groups'].append({'group_id':ident,'members':names,'satisfaction_rule':rule,'note':note})


def build():
    recipes=[]
    r=recipe('宫保鸡丁','data/HowToCook/dishes/meat_dish/宫保鸡丁/宫保鸡丁.md')
    group(r,'meat',['手枪腿','鸡胸脯肉'],'any_one','原文明确“或者”；不能因拥有鸡胸脯肉而要求再买手枪腿。')
    group(r,'chili',['干辣椒','二荆条'],'any_one','复杂版本推荐二荆条；两项不是必须同时具备。')
    for name in ['手枪腿','鸡胸脯肉']:
        add(r,name,'ingredient',[o(11),o(37,quantity='1 支（约 350g）'),o(66,'step_usage',version='简易版本',condition='若是鸡胸脯肉，则可以直接进行切丁以及之后的动作'),o(82,'step_usage',version='稍加复杂但是更加令人垂涎欲滴的版本')],group='meat',note='计算条目的支数/重量属于替代组原文，不自动转换到每个选项。')
    add(r,'大葱','ingredient',[o(12),o(38,quantity='1 根（约 180g）'),o(67,'step_usage',version='简易版本')],note='葱绿、葱白、葱粒为形态/部位提及，不作为库存可互换的别名。')
    for name in ['干辣椒','二荆条']:
        add(r,name,'seasoning',[o(13),o(41,quantity='10g（若选择二荆条，则需要大约 4 支）'),o(69,'step_usage',version='简易版本') if name=='干辣椒' else o(81,'step_usage',version='稍加复杂但是更加令人垂涎欲滴的版本')],group='chili')
    add(r,'熟花生','ingredient',[o(14),o(39,quantity='150g'),o(72,'step_usage',version='简易版本')],note='与生花生分别记录，不能直接用同一个库存名称覆盖。')
    pairs=[('生抽酱油',15,42,'10g'),('白糖',17,43,'2g'),('料酒',18,47,'15g'),('盐',19,44,'2g'),('淀粉',21,46,'15g'),('植物油',22,45,'20g')]
    for name,line,calc,qty in pairs:
        obs=[o(line),o(calc,quantity=qty)]
        if name=='淀粉':obs += [o(54,'advanced_list',quantity='10g',stage='勾芡'),o(73,'step_usage',quantity='淀粉 10g 加 50g 清水',stage='勾芡',version='简易版本')]
        add(r,name,'seasoning',obs)
    for name,line,calc,qty,step in [('香醋',16,51,'5g',72),('鸡精',20,52,'2g',72),('芝麻油',23,53,'10g',74)]:
        add(r,name,'seasoning',[o(line),o(calc,'advanced_list',quantity=qty),o(step,'step_usage',version='简易版本')],note='计算列为进阶配料，但总原料表及简易操作也出现；不按计算分组强行排除。')
    add(r,'生姜','ingredient',[o(40,quantity='10g'),o(67,'step_usage',quantity='姜片 5g',version='简易版本'),o(83,'step_usage',quantity='姜片 5g',version='稍加复杂但是更加令人垂涎欲滴的版本')],aliases=['姜片'],note='总原料表未列出，计算和两版步骤均有。')
    for name,line,qty,step in [('老抽酱油',49,'5g',68),('花椒',50,'5g',69)]:
        add(r,name,'seasoning',[o(line,'advanced_list',quantity=qty),o(step,'step_usage',version='简易版本'),o(84 if name=='老抽酱油' else 89,'step_usage',version='稍加复杂但是更加令人垂涎欲滴的版本')],note='总原料表缺失，计算进阶分组与简易步骤存在差异。')
    for name,line,calc,qty,step in [('油泼辣子',27,58,'5g',91),('莴笋',28,57,'约 250g',80),('生花生',29,None,'',85),('豆瓣酱',30,55,'10g',86)]:
        obs=[o(line,'optional')]
        if calc:obs.append(o(calc,'optional' if name in ('油泼辣子','莴笋') else 'advanced_list',quantity=qty))
        obs.append(o(step,'step_usage',version='稍加复杂但是更加令人垂涎欲滴的版本'))
        add(r,name,'ingredient' if name in ('莴笋','生花生') else 'seasoning',obs,note='可选来自子标题；复杂版本步骤使用，不视为所有版本都必需。')
    add(r,'水','ingredient',[o(66,'step_usage',stage='浸泡',version='简易版本',condition='泡于清水 10 分钟'),o(67,'step_usage',quantity='50g 开水',stage='葱姜水',version='简易版本'),o(71,'conditional',condition='加入余下葱姜水不够 100g 再加一点清水（务必是热水）',version='简易版本'),o(73,'step_usage',quantity='50g 清水',stage='水淀粉',version='简易版本')],aliases=['清水','开水','热水'],note='浸泡10分钟作为工序条件保留，不当作水量；水温要求不因拥有水而自动满足。')
    for name,line in [('剪刀',66),('刀',66),('碗',67),('微波炉',67),('保鲜膜',68),('冰箱',68),('锅铲',70),('竹筷子',70)]:
        add(r,name,'tool',[o(line,'step_usage',version='简易版本')])
    for name,line,stage in [('葱姜水',67,'调制葱姜水'),('鸡丁',68,'腌制'),('水淀粉',73,'勾芡')]:
        add(r,name,'intermediate',[o(line,'step_usage',stage=stage,version='简易版本')])
    r['issues']=[{'kind':'version_conflict','note':'计算“进阶配料”中的老抽、花椒、香醋、鸡精、芝麻油，简易操作同样使用；不能直接把计算分组映射成版本必需集合。','lines':[48,49,50,51,52,53,68,69,72,74]},
                 {'kind':'optional_scope','note':'子标题可选与复杂版本步骤并存，属性必须有版本/章节范围。','lines':[25,27,28,29,30,80,85,86,91]}]
    # 用户确认：按所选版本步骤提示备料，同时保留原料表的差异。
    simple='简易版本'
    complex_version='稍加复杂但是更加令人垂涎欲滴的版本'
    additions={
        '大葱':[(71,simple),(83,complex_version),(88,complex_version)],
        '白糖':[(72,simple),(89,complex_version)],
        '料酒':[(68,simple),(84,complex_version)],
        '盐':[(68,simple),(84,complex_version)],
        '淀粉':[(68,simple),(84,complex_version),(90,complex_version)],
        '植物油':[(70,simple),(85,complex_version)],
        '香醋':[(89,complex_version)],'鸡精':[(89,complex_version)],
        '芝麻油':[(91,complex_version)],
        '水':[(82,complex_version),(83,complex_version),(88,complex_version),(90,complex_version)],
        '莴笋':[(87,complex_version)],'二荆条':[(88,complex_version)],
    }
    for entity in r['entities']:
        for line,version in additions.get(entity['canonical_name'],[]):
            stage='勾芡' if entity['canonical_name']=='淀粉' and line==90 else None
            condition='葱姜水不够 100g 时补热水' if entity['canonical_name']=='水' and line==88 else None
            entity['observations'].append({
                'requirement':'conditional' if condition else 'step_usage',
                'quantity_raw':None,'stage':stage,'version':version,
                'condition_raw':condition,'purpose':None,'source':r['source'],
                'section':r['_sections'][line],'start_line':line,'end_line':line,
                'quote':r['_lines'][line-1]})
    r['issues'].append({'kind':'table_only','note':'生抽酱油在原料表和计算中列出，但两版操作未明确使用；保留待确认提示，不擅自改成老抽或判定可省。','lines':[15,42,68,84]})
    r['answer_policy']={
        'status':'user_confirmed_for_this_sample',
        'basis':'按所选版本步骤提示需要补充的用料，同时展示原料表差异。',
        'missing_inventory':'未提供库存时只列备料依据，不声称用户缺少哪些食材。',
        'alternatives':'替代组满足一个选项即可；不按成员重复计数。',
        'limitations':'步骤使用表示按原文执行的备料依据，不推断所有做法无条件必需；不换算数量，不自动推导生熟花生互换。'}
    r['version_preparation']=[]
    for version in (simple,complex_version):
        entries=[]
        for entity in r['entities']:
            if entity['category'] not in ('ingredient','seasoning'):continue
            refs=[obs['start_line'] for obs in entity['observations'] if obs['version']==version]
            if refs:
                entries.append({'entity_id':entity['entity_id'],'name':entity['canonical_name'],
                    'alternative_group':entity['alternative_group'],'evidence_lines':sorted(set(refs))})
        r['version_preparation'].append({'version':version,'basis':'该版本步骤使用；替代关系另按原文替代组判断',
            'entries':entries,'table_difference_issue_kinds':['version_conflict','optional_scope','table_only']})
    recipes.append(r)

    r=recipe('基础牛奶面包','data/HowToCook/dishes/staple/基础牛奶面包/基础牛奶面包.md')
    group(r,'flour',['高筋面粉','中筋面粉'],'any_one','优先高筋、避免低筋；不将低筋并入面粉同义词。')
    group(r,'sweetener',['糖','糖浆'],'any_one')
    group(r,'dairy',['牛奶','淡奶油','炼乳','奶粉'],'one_or_more','原文允许混合；避免复方奶粉和额外含糖的原料。')
    group(r,'fat',['黄油','玉米油'],'conditional_any_one','奶制品含脂量极高可以不加；不能自动判断用户牛奶满足条件。')
    group(r,'liner',['硅油纸','模具'],'any_one','作为原文工具栏替代项记录；不据此保证两者在任意步骤完全等价。')
    for name in ['高筋面粉','中筋面粉']:
        add(r,name,'ingredient',[o(15),o(65,quantity='1 cup',stage='酵头'),o(70,quantity='2½ cup',stage='面团')],group='flour',note='计算中的“面粉”回指本菜谱允许的面粉组。')
    add(r,'鸡蛋','ingredient',[o(16),o(71,quantity='1 个',stage='面团')])
    for name in ['糖','糖浆']:
        add(r,name,'seasoning',[o(17),o(72,quantity='⅛ cup',stage='面团')],group='sweetener')
    add(r,'干酵母','ingredient',[o(18),o(67,quantity='2 g',stage='酵头')],aliases=['酵母'],note='此菜谱局部回指；不推广为所有酵母制品全局等价。')
    add(r,'盐','seasoning',[o(19),o(68,quantity='2 g',stage='酵头')])
    for name in ['牛奶','淡奶油','炼乳','奶粉']:
        add(r,name,'ingredient',[o(20),o(73,quantity='混合后共 ¼ cup',stage='面团',condition='奶粉需要和水混合。')],group='dairy')
    for name in ['黄油','玉米油']:
        add(r,name,'ingredient',[o(21,'conditional',condition='如果选用的奶制品含脂量极高可以不加'),o(74,'listed_required',quantity='⅛ cup',stage='面团')],group='fat',note='计算条目未重复条件，保留两个观察，不取消原料栏条件。')
    for name,line,calc,qty in [('谷朊粉',22,75,'¼ ~ ½ cup'),('香草精',23,76,'3 g')]:
        add(r,name,'ingredient' if name=='谷朊粉' else 'seasoning',[o(line,'optional'),o(calc,'optional',quantity=qty,stage='面团')])
    add(r,'水','ingredient',[o(66,quantity='30 ℃ 温水（以不烫手为宜） 1 cup',stage='酵头'),o(86,'step_usage',quantity='30℃ 的温水',stage='酵头'),o(73,'conditional',stage='面团',condition='奶粉需要和水混合。'),o(129,'conditional',stage='面团',condition='面团成型但却比较硬，说明水分过少'),o(137,'conditional',stage='面团',condition='发酵后的面团表面没有粘性')],aliases=['温水'],note='明确收录数字开头计算项；水温和体积不混为一个数值。水的形态名称用于归类，不表示现有水已满足温度条件。')
    for name,line in [('烤箱',27),('量杯',28),('布',29),('擀面杖',30)]:
        add(r,name,'tool',[o(line)],note='烤箱需要控温' if name=='烤箱' else '')
    for name in ['硅油纸','模具']:add(r,name,'tool',[o(31),o(142,'step_usage',stage='整形')],group='liner')
    for name,line in [('刮刀',40),('刮板',43),('喷壶',45),('筛子',46),('面板',47),('刷子',48),('烤盘',142)]:
        add(r,name,'tool',[o(line,'mentioned',stage='工具介绍' if line<54 else '整形')],aliases=['硅胶铲'] if name=='刮刀' else [])
    add(r,'植物油','ingredient',[o(133,'step_usage',stage='面团发酵',purpose='容器防粘')],note='工序用途与面团中的油脂配料分开，不默认黄油/玉米油库存可以全部满足。')
    add(r,'蛋液','intermediate',[o(155,'optional',stage='烘烤前',purpose='表面涂刷')],note='不作为另需购买一种独立食材；原文未给出用量或来源比例。')
    for name,line,stage in [('酵头',64,'酵头'),('面团',69,'面团'),('面包',167,'成品')]:
        add(r,name,'intermediate' if name!='面包' else 'finished_product',[o(line,'mentioned',stage=stage)])
    r['issues']=[{'kind':'conditional_scope','note':'奶制品油脂条件尚需用户确认；计算条目不自动覆盖条件。','lines':[21,74]},
                 {'kind':'purpose_scope','note':'防粘用植物油、刷面蛋液与面团配料用途不同，不扁平叠加。','lines':[133,144,155]},
                 {'kind':'excluded_scope','note':'故障排查、永久酵头维护、换算表和附加食用方式不并入基础采购清单。','lines':[96,105,173,190]}]
    r['answer_policy']={
        'status':'user_authorized_sample_integration',
        'basis':'基础配料、可选项、油脂条件及防粘用途分别判断；工具与中间产物单列。',
        'limitations':'不推断奶制品含脂量，不判断库存数量，不自动转换温度、单位或采购数量。'}
    r['version_preparation']=[{'version':None,'basis':'原文基础做法',
        'entries':[{'entity_id':e['entity_id'],'name':e['canonical_name'],
            'alternative_group':e['alternative_group'],
            'evidence_lines':sorted({obs['start_line'] for obs in e['observations']})}
            for e in r['entities'] if e['category'] in ('ingredient','seasoning')]}]
    recipes.append(r)

    r=recipe('凉拌黄瓜','data/HowToCook/dishes/vegetable_dish/凉拌黄瓜.md')
    for name,line,calc,qty,category in [('黄瓜',11,22,'200 克 * 份数','ingredient'),('醋',12,23,'7.5 ml + 4 ml * 份数','seasoning'),('酱油',13,24,'5 ml + 2.5 ml * 份数','seasoning'),('蒜',14,25,'3 瓣 * 份数','ingredient')]:
        add(r,name,category,[o(line),o(calc,quantity=qty),o(34 if name=='蒜' else 32 if name=='黄瓜' else 36,'step_usage')],aliases=['大蒜'] if name=='蒜' else [])
    for name,line,qty,step in [('盐',26,'0.4 克 + 0.2 克 * 份数',36),('香油',27,'5 ml + 2 ml * 份数',37),('蚝油',28,'5 ml',36)]:
        add(r,name,'seasoning',[o(line,quantity=qty),o(step,'step_usage')],note='原料表漏列，计算和操作均明确出现。')
    add(r,'白糖','seasoning',[o(35,'step_usage',quantity='5 克',stage='腌制',purpose='腌制黄瓜')],note='原料表与计算均未列，操作明确加入；原文未标可选，不按“更好吃”自行改成可选。')
    for name,line in [('菜刀',32),('碗',33)]:add(r,name,'tool',[o(line,'step_usage')])
    add(r,'冰箱','tool',[o(42,'conditional',condition='亦可先准备好后放入冰箱冷藏')])
    add(r,'保鲜膜','tool',[o(43,'conditional',condition='若放入冰箱冷藏，则需使用保鲜膜包紧')])
    r['issues']=[{'kind':'missing_from_table','note':'盐、香油、蚝油需从计算/操作补充，白糖仅在操作出现。','lines':[11,12,13,14,26,27,28,35,36,37]},
                 {'kind':'alias','note':'大蒜/蒜为本样本人工整理的局部规范名称，原词保留。','lines':[14,25,34]},
                 {'kind':'selection_logic','note':'指定凉拌黄瓜时不应转推荐凉皮；已在本样本试验分支固定指定菜名，一般推荐逻辑尚未整体修复。','lines':[1]}]
    r['answer_policy']={
        'status':'user_authorized_sample_integration',
        'basis':'按原文基础步骤核对库存，同时提示原料表与计算/步骤差异。',
        'aliases':'蒜/大蒜仅在本样本中用于匹配。',
        'limitations':'不判断库存数量或工具是否齐全；白糖未标可选，不擅自省略。'}
    r['version_preparation']=[{'version':None,'basis':'原文基础做法',
        'entries':[{'entity_id':e['entity_id'],'name':e['canonical_name'],
            'alternative_group':e['alternative_group'],
            'evidence_lines':sorted({obs['start_line'] for obs in e['observations']})}
            for e in r['entities'] if e['category'] in ('ingredient','seasoning')]}]
    recipes.append(r)
    return recipes


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    data=build()
    preview=['# 三篇菜谱结构化样本（待用户核对）\n\n人工整理的离线草稿。未写入 Milvus，未接入正式推荐。引用位置及原文由程序核对；语义分类和关系仍待用户确认。数量保留原文，不转换单位、不按替代成员重复累加。\n\n']
    for r in data:
        del r['_lines'];del r['_sections']
        upgrade(r)
        validate(r)
        r['validation']['structural_status']='passed'
        (OUT/(r['recipe_id']+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
        preview.append(f"## {r['title']}\n\n原文：[打开]({ROOT/r['source']})\n\n")
        preview.append('| 名称 | 类别 | 各章节属性 / 阶段 / 版本 | 替代组 | 原文依据 |\n|---|---|---|---|---|\n')
        labels={'ingredient':'食材','seasoning':'调料','tool':'工具','intermediate':'中间产物','finished_product':'成品','listed_required':'列表要求','optional':'可选','conditional':'有条件','advanced_list':'计算进阶分组','step_usage':'步骤使用','mentioned':'仅提及'}
        for e in r['entities']:
            scopes=[];links=[]
            for obs in e['observations']:
                scope=labels[obs['requirement']]+''.join('/'+obs[k] for k in ('stage','version') if obs[k])
                if scope not in scopes:scopes.append(scope)
                link=f"[L{obs['start_line']}]({ROOT/r['source']}:{obs['start_line']})"
                if link not in links:links.append(link)
            preview.append(f"| {e['canonical_name']} | {labels[e['category']]} | {'；'.join(scopes)} | {e['alternative_group'] or '—'} | {' '.join(links)} |\n")
        preview.append('\n### 替代关系\n\n')
        for g in r['alternative_groups']:preview.append(f"- `{g['group_id']}`：{' / '.join(g['members'])}；规则 `{g['satisfaction_rule']}`。{g['note']}\n")
        preview.append('\n### 差异与待确认\n\n')
        for issue in r['issues']:preview.append(f"- {issue['note']}\n")
        if r.get('version_preparation'):
            preview.append('\n### 按版本备料依据（样本接入规则）\n\n按所选版本步骤提示用料，原料表差异仍保留。没有库存时不生成“缺料”结论。替代组选项满足一种即可；用途和工序条件仍须查看原文。\n\n')
            for plan in r['version_preparation']:
                preview.append(f"#### {plan['version'] or '原文基础做法'}\n\n")
                shown=set()
                for entry in plan['entries']:
                    gid=entry['alternative_group']
                    if gid and gid in shown:continue
                    if gid:
                        shown.add(gid)
                        g=next(g for g in r['alternative_groups'] if g['group_id']==gid)
                        name=' / '.join(g['members'])+'（任选一种；具体版本用法见引用）'
                    else:name=entry['name']
                    links=' '.join(f"[L{line}]({ROOT/r['source']}:{line})" for line in entry['evidence_lines'])
                    preview.append(f"- {name}：{links}\n")
                preview.append('\n')
        preview.append('\n### 条件、数量与逐字原文\n\n')
        for e in r['entities']:
            preview.append(f"#### {e['entity_id']} {e['canonical_name']}\n\n")
            if e['aliases']:preview.append('本菜谱别名：'+ '、'.join(e['aliases'])+'。\n\n')
            if e['review_note']:preview.append(e['review_note']+'\n\n')
            for obs in e['observations']:
                preview.append(f"- {obs['section']} · L{obs['start_line']} · {labels[obs['requirement']]}；数量原文：{obs['quantity_raw'] or '未提供'}；条件：{obs['condition_raw'] or '未单列'}；用途：{obs['purpose'] or '未单列'}\n\n> {obs['quote']}\n\n")
    (OUT/'review-preview.md').write_text(''.join(preview))
    print('已建立三篇人工整理草稿：',[(r['title'],len(r['entities'])) for r in data])

if __name__=='__main__':main()
