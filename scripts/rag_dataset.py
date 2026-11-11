"""选择当前知识库，环境变量可临时切回测试集合。"""
import json
import os
from check_embedding import ROOT

settings_path = ROOT / 'data/processed/active-dataset.json'
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
DATASET = os.environ.get('RAG_DATASET', settings.get('dataset', 'test_dishes'))
if DATASET not in ('test_dishes', 'all_recipes'):
    raise ValueError('未知知识库。')
FOLDER = ROOT / 'data/processed' / DATASET
COLLECTION = {'test_dishes': 'recipe_test_v1', 'all_recipes': 'recipe_all_v1'}[DATASET]
VERSION = os.environ.get('RAG_VERSION', settings.get('version', ''))
if VERSION:
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_]+', VERSION):
        raise ValueError('知识库版本名无效')
    FOLDER = ROOT / 'data/processed/recipe_versions' / VERSION
    plan = json.loads((FOLDER / 'version-plan.json').read_text())
    report = json.loads((ROOT/'data/processed/index-lab'/(plan['collection']+'.json')).read_text())
    if report.get('status')!='validated' or report.get('source_sha256') != json.loads((FOLDER/'embedding-metadata.json').read_text())['source_sha256']:
        raise ValueError('新知识库版本尚未完成入库核对')
    COLLECTION = plan['collection']
