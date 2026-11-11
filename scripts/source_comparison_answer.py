"""有限来源版本绑定的章节对照与烘烤步骤；不把推测当作原文解释。"""
import hashlib
import json
import re

from check_embedding import ROOT
from retrieval_candidates import match_titles

VIEWS = ROOT / 'data/structured/comparison-views-v1.json'


def select_comparison_plan(question, chunks):
    chunks = list(chunks)
    comparison = bool(re.search(r'还是|差异|区别|不同|比较|相较', question))
    baking = bool(re.search(r'面团', question) and re.search(r'烘烤|烤制', question)
                  and re.search(r'怎样|怎么|如何', question))
    if not comparison and not baking: return None
    if re.search(r'品牌|过敏|忌口|替代|替换|几份|多少|用量|保存|存放|缺|推荐|中心', question): return None
    titles = match_titles(question, {r['title'] for r in chunks})
    if len(titles) != 1: return None
    profile = next((p for p in json.loads(VIEWS.read_text())['profiles']
                    if p['title'] == titles[0] and all(any(term in question for term in group)
                    for group in p['query_term_groups'])
                    and (baking if p.get('intent') == 'baking_stages' else comparison)), None)
    if profile is None: return None
    numbers = re.findall(r'\d+(?:\.\d+)?', question)
    if numbers:
        # 仅放行原文已有数字的章节对照，不将自拟温度/时长带入有限规则。
        original_numbers = {number for claim in profile['claims'] for item in claim['evidence']
                            for number in re.findall(r'\d+(?:\.\d+)?', item['quote'])}
        if (baking or not re.search(r'原文|章节', question)
                or not set(numbers) <= original_numbers):
            return None
    if profile.get('intent') == 'baking_stages' and re.search(r'调整|预热|不变色|发苦|开裂|内部|熟|覆盖|替代', question):
        return None
    if {r['source'] for r in chunks if r['title'] == titles[0]} != {profile['source']}: return None
    path = (ROOT / profile['source']).resolve()
    if not path.is_relative_to((ROOT / 'data/HowToCook/dishes').resolve()):
        raise ValueError('章节对照来源路径越界')
    if hashlib.sha256(path.read_bytes()).hexdigest() != profile['source_sha256']:
        raise ValueError('章节对照与原文版本不同，需重新核对')
    lines = path.read_text().splitlines()
    for claim in profile['claims']:
        for e in claim['evidence']:
            if not 1 <= e['line'] <= len(lines) or lines[e['line']-1] != e['quote']:
                raise ValueError('章节对照原文摘录不匹配')
    return profile


def prepare_comparison_answer(plan, references):
    claims = []
    for claim in plan['claims']:
        evidence = []
        for item in claim['evidence']:
            line = item['line']
            ref = next((r for r in references if r['source'] == plan['source'] and
                        r['content_start_line'] <= line < r['content_start_line'] + len(r['content'].splitlines())), None)
            if ref is None: raise ValueError('章节对照缺少必要原文')
            evidence.append({'reference_id': ref['reference_id'], 'start_line': line, 'end_line': line})
        claims.append({'text': claim['text'], 'evidence': evidence})
    return {'claims': claims, 'missing_information': ''}


def validate_comparison_answer(answer, plan, references):
    if json.loads(answer) != prepare_comparison_answer(plan, references):
        raise ValueError('章节对照输出与来源规则不同')
