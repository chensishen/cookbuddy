"""按来源聚合活动知识库元信息；目录查找不是食材推荐或配方认证。"""
from collections import Counter
from pathlib import PurePosixPath

from rag_dataset import DATASET, VERSION, COLLECTION

CATEGORIES = {'aquatic':'水产', 'breakfast':'早餐', 'condiment':'调料酱汁',
              'dessert':'甜品', 'drink':'饮品', 'meat_dish':'荤菜',
              'semi-finished':'半成品', 'soup':'汤羹', 'staple':'主食',
              'vegetable_dish':'素菜'}


def bind_source(question, title, source, chunks):
    from retrieval_candidates import match_titles
    chunks = list(chunks)
    chosen = [row for row in chunks if row['source']==source]
    if not chosen:
        raise ValueError('所选来源不在当前知识库')
    # 路径目录名可能是另一道菜的名字；用户明确选择的路径不作为额外菜名意图。
    mentioned = match_titles(question.replace(source, ''), {row['title'] for row in chunks})
    if any(name!=chosen[0]['title'] for name in mentioned):
        raise ValueError('问题中的菜名与所选来源不一致，请取消来源选择或修改问题')
    if title and title not in (chosen[0]['title'], source):
        raise ValueError('菜名与所选来源不一致')
    if any(row['source'] in question and row['source']!=source for row in chunks):
        raise ValueError('问题已经指定其他来源，请取消来源选择或修改问题')
    effective = question+'\n指定菜谱原文来源：'+source
    if len(effective)>2000:
        raise ValueError('问题过长，请缩短后再选择来源')
    return effective


def build_catalog(chunks):
    grouped = {}
    for row in chunks:
        source = row['source']
        parts = PurePosixPath(source).parts
        prefix = ('data', 'HowToCook', 'dishes')
        relative = parts[len(prefix):] if parts[:len(prefix)] == prefix else parts[-1:]
        category = relative[0] if len(relative) > 1 else 'other'
        label = CATEGORIES.get(category, '其他')
        item = grouped.setdefault(source, {'title': row['title'], 'source': source,
            'category': category, 'category_label': label,
            'source_label': label+' / '+' / '.join(relative[1:] if len(relative)>1 else relative),
            'sections': [], 'chunk_count': 0})
        if item['title'] != row['title']:
            raise ValueError('同一来源出现不同菜名，需核对知识库元信息')
        item['chunk_count'] += 1
        if row['section'] not in item['sections']:
            item['sections'].append(row['section'])
    duplicates = Counter(item['title'] for item in grouped.values())
    for item in grouped.values():
        item['same_title_sources'] = duplicates[item['title']]
    return sorted(grouped.values(), key=lambda item: (item['title'], item['source']))


def catalog_page(chunks, query='', category='', offset=0, limit=12):
    if not isinstance(query, str) or len(query) > 100 or not isinstance(category, str):
        raise ValueError('菜谱关键词最多100字')
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 100000 or not 1 <= limit <= 50:
        raise ValueError('分页范围无效')
    items = build_catalog(chunks)
    counts = Counter(item['category'] for item in items)
    if category and category not in counts:
        raise ValueError('菜谱分类不存在')
    terms = query.strip().casefold().split()
    matched = [item for item in items if (not category or item['category']==category)
        and all(term in (item['title']+' '+' '.join(item['sections'])).casefold() for term in terms)]
    return {'dataset': DATASET, 'version': VERSION, 'collection': COLLECTION,
            'source_count': len(items), 'title_count': len({item['title'] for item in items}),
            'total': len(matched), 'offset': offset, 'limit': limit,
            'items': matched[offset:offset+limit],
            'categories': [{'id': name, 'label': CATEGORIES.get(name, '其他'), 'count': count}
                           for name, count in sorted(counts.items())]}
