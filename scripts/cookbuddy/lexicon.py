"""Meal vocabulary shared by model prompt, deterministic parser and generated tests."""
from functools import lru_cache
from pathlib import Path
import json
import re
import yaml


@lru_cache(maxsize=1)
def settings():
    return yaml.safe_load((Path(__file__).resolve().parents[2]/'config/lexicon.yaml').read_text())


def meal_match(question):
    from src.nlu.normalize import normalize
    from src.nlu.matching import fuzzy_match
    text=normalize(question)['text'];cfg=settings()
    aliases={word:kind for kind,item in cfg['meal_types'].items() for word in item['synonyms']}
    matches=[word for word in aliases if word in text]
    kinds={aliases[word] for word in matches}
    if len(kinds)>1:return {'meal_type':'unspecified','ambiguous':True,'recognized':True}
    if kinds:return {'meal_type':kinds.pop(),'ambiguous':False,'recognized':True}
    cleaned=text
    for word in sorted(cfg['fillers']+cfg['intents']['plan_menu']['triggers'],key=len,reverse=True):cleaned=cleaned.replace(word,'')
    cleaned=re.sub(r'[\W\d\s]+','',cleaned)
    if 2<=len(cleaned)<=4:
        matched=fuzzy_match(cleaned,list(aliases),fillers=False)
        if matched['value']:return {'meal_type':aliases[matched['value']],'ambiguous':False,'recognized':True}
    return {'meal_type':'unspecified','ambiguous':False,'recognized':False}


def planning_request(question):
    match=meal_match(question)
    triggers=settings()['intents']['plan_menu']['triggers']
    requested=any(word in question for word in triggers)
    return match if requested and (match['recognized'] or any(word in question for word in ('菜单','一餐','一顿','吃什么'))) else None


def strip_meal_words(text):
    cfg=settings();words=cfg['fillers']+cfg['intents']['plan_menu']['triggers']
    words+= [word for row in cfg['meal_types'].values() for word in row['synonyms']]
    # Match typo spans through the same bounded matcher rather than deleting arbitrary words.
    match=meal_match(text)
    if match['recognized'] and not any(word in text for row in cfg['meal_types'].values() for word in row['synonyms']):
        cleaned=text
        for word in sorted(words,key=len,reverse=True):cleaned=cleaned.replace(word,'')
        if 2<=len(re.sub(r'[\W\d\s]+','',cleaned))<=4:return ''
    for word in sorted(set(words),key=len,reverse=True):text=text.replace(word,'')
    return text


def prompt_instructions():
    cfg=settings()
    return '餐次槽位meal.meal_type依据以下唯一配置提取；不明确为unspecified，不猜人数或长期条件：'+json.dumps(cfg,ensure_ascii=False)
