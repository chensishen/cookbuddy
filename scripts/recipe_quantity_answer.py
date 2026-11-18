"""来源绑定的有限公式计算；Decimal与白名单AST，不执行模型代码。"""
import ast
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import re

from check_embedding import ROOT
from retrieval_candidates import match_titles

PROFILES = ROOT / 'data/structured/quantity-profiles-v1.json'


def evaluate_formula(expression, variables):
    if not isinstance(expression, str) or len(expression) > 120: raise ValueError('公式过长或类型错误')
    try: tree = ast.parse(expression, mode='eval')
    except SyntaxError: raise ValueError('公式语法无效') from None
    if len(list(ast.walk(tree))) > 40: raise ValueError('公式过于复杂')
    def value(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = Decimal(str(node.value))
            if not result.is_finite(): raise ValueError('公式常量无效')
            return result
        if isinstance(node, ast.Name) and node.id in ('servings', 'eggs') and node.id in variables:
            try: result = Decimal(str(variables[node.id]))
            except (InvalidOperation, ValueError, TypeError): raise ValueError('变量无效') from None
            if not result.is_finite(): raise ValueError('变量无效')
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = value(node.left), value(node.right)
            if isinstance(node.op, ast.Add): return a+b
            if isinstance(node.op, ast.Sub): return a-b
            if isinstance(node.op, ast.Mult): return a*b
            if b == 0: raise ValueError('公式除以零')
            return a/b
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'ceil'
                and len(node.args) == 1 and not node.keywords):
            return value(node.args[0]).to_integral_value(rounding=ROUND_CEILING)
        raise ValueError('公式含未授权的运算或名称')
    result = value(tree.body)
    if not result.is_finite() or result < 0 or result > Decimal('1000000'): raise ValueError('计算结果越界')
    return result


def format_number(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def parse_servings(question):
    if re.search(r'\d+\.\d+|半份|分之|/|[～~]|\d\s*-\s*\d|到|至|或者|分别|或|两种', question): return None
    matches = re.findall(r'([0-9]+|[一二两三四五六七八九十]+)\s*(?:人份|份|个?人)', question)
    if len(matches) != 1: return None
    raw = matches[0]; digits = dict(zip('一二两三四五六七八九','1223456789'))
    if raw.isascii(): count = int(raw)
    elif raw in digits: count = int(digits[raw])
    elif raw == '十': count = 10
    elif re.fullmatch(r'[二三四五六七八九]?十[一二三四五六七八九]?',raw):
        a,b = raw.split('十'); count = int(digits[a]) * 10 if a else 10
        count += int(digits[b]) if b else 0
    else: return None
    return count if 1 <= count <= 100 else None


def select_quantity_plan(question, chunks):
    chunks = list(chunks)
    question = re.sub(r'[。！？!?；;]\s*(?:(?:请)?(?:给出|列出|附上)原文(?:公式(?:或依据)?|依据)|原文(?:没|没有)写换算方法的话(?:请)?直说)[。！？!? ]*$', '', question)
    if not re.search(r'多少|几个|用量|需要|各.*量', question): return None
    if re.search(r'过敏|忌口|替代|替换|缺|没有|我有|只有|库存|够不够|剩|换算|公斤|kg|毫克|mg|汤匙|勺|怎么做|步骤|温度|分钟|保存',question,re.I): return None
    servings = parse_servings(question)
    if servings is None: return None
    titles = match_titles(question,{r['title'] for r in chunks})
    if len(titles) != 1: return None
    profile = next((p for p in json.loads(PROFILES.read_text())['profiles'] if p['title'] == titles[0]),None)
    if profile is None: return None
    if {r['source'] for r in chunks if r['title'] == titles[0]} != {profile['source']}: return None
    source = (ROOT/profile['source']).resolve()
    if not source.is_relative_to((ROOT/'data/HowToCook/dishes').resolve()): raise ValueError('数量公式路径越界')
    if hashlib.sha256(source.read_bytes()).hexdigest() != profile['source_sha256']: raise ValueError('数量公式与原文版本不同')
    lines = source.read_text().splitlines()
    items = [e['evidence'] for e in profile['entries']] + [profile['portion_evidence']]
    items += profile.get('optional_evidence',[])
    if profile.get('unscaled_step'): items.append(profile['unscaled_step']['evidence'])
    if any(not 1 <= e['line'] <= len(lines) or lines[e['line']-1] != e['quote'] for e in items): raise ValueError('数量公式的逐字依据不匹配')
    # 菜名不能被当作所问食材：先去掉菜名，再识别明确项目。
    request = question.replace(profile['title'],'')
    if profile['title'] == '西红柿炒鸡蛋' and re.search(r'酱油|香油|蚝油|橄榄油|植物油|黄油|玉米油',request): return None
    all_items = bool(re.search(r'各种|各.*多少|用料|配料|总量|食材|调料',request))
    wanted = {e['name'] for e in profile['entries'] if any(n in request for n in [e['name']]+e['aliases'])}
    unscaled = profile.get('unscaled_step')
    step_wanted = bool(unscaled and (all_items or unscaled['name'] in request))
    if all_items: wanted = {e['name'] for e in profile['entries']}
    if not wanted and not step_wanted: return None
    variables = {'servings': Decimal(servings)}; calculations = []
    egg_evidence = None
    for entry in profile['entries']:
        values = [evaluate_formula(expr,variables) for expr in entry['expressions']]
        if entry['bind']:
            variables[entry['bind']] = values[0]; egg_evidence = entry['evidence']
        if entry['name'] not in wanted: continue
        substitutions = []
        for expr in entry['expressions']:
            text = expr
            for name, val in variables.items(): text = re.sub(r'\b'+name+r'\b',format_number(val),text)
            text = text.replace('*',' × ').replace('+',' + ')
            if text.startswith('ceil('): text = text[5:-1]+'（向上取整）'
            substitutions.append(text)
        evidence = [profile['portion_evidence'],entry['evidence']]
        if any('eggs' in expr for expr in entry['expressions']): evidence.append(egg_evidence)
        if entry['optional']: evidence += profile['optional_evidence']
        result = '～'.join(format_number(v) for v in values)
        formula = '～'.join(substitutions)
        text = entry['name']+'：'+formula+' = '+result+entry['unit']+'。'
        if any('eggs' in expr for expr in entry['expressions']):
            text += '鸡蛋先按1.5 × '+str(servings)+'向上取整为'+format_number(variables['eggs'])+'个，再据此计算油。'
        if entry['optional']: text += '原文标为可选，不是必须达到该范围上限。'
        if not any('servings' in x or 'eggs' in x for x in entry['expressions']): text += '原文为固定量，不按份数乘倍。'
        calculations.append({'text':text,'evidence':evidence,'name':entry['name'],'values':[format_number(v) for v in values]})
    if step_wanted:
        calculations.append({'text':'步骤另写白糖5克，但原文没有按份数的白糖公式；不能据此确定'+str(servings)+'份白糖应为多少，不自动乘倍。','evidence':[unscaled['evidence']],'name':'白糖','values':[]})
    return {'source':profile['source'],'source_sha256':profile['source_sha256'],'servings':servings,'calculations':calculations}


def prepare_quantity_answer(plan,references):
    claims=[]
    # 每个计算独立引用，避免同一句中的公式与不同条目串位。
    for calc in plan['calculations']:
        evidence=[]
        for item in calc['evidence']:
            line=item['line']
            row=next((r for r in references if r['source']==plan['source'] and r['content_start_line']<=line<r['content_start_line']+len(r['content'].splitlines())),None)
            if row is None: raise ValueError('数量计算缺少必要原文依据')
            evidence.append({'reference_id':row['reference_id'],'start_line':line,'end_line':line})
        claims.append({'text':calc['text'],'evidence':evidence})
    return {'claims':claims,'missing_information':''}


def validate_quantity_answer(answer,plan,references):
    if json.loads(answer)!=prepare_quantity_answer(plan,references): raise ValueError('数量回答与来源公式计算不一致')
