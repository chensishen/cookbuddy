"""按明确菜名补召回，并合并来源而不改写向量相似度。"""

def normalize_recipe_aliases(question):
    for alias, canonical in (('番茄炒蛋','西红柿炒鸡蛋'), ('番茄炒鸡蛋','西红柿炒鸡蛋')):
        question = question.replace(alias, canonical)
    return question


def match_titles(question, titles):
    question = normalize_recipe_aliases(question)
    matches = []
    for title in sorted(set(titles), key=lambda t: (-len(t), t)):
        if len(title) < 2:
            continue
        offset = 0
        while True:
            start = question.find(title, offset)
            if start < 0:
                break
            end = start + len(title)
            if not any(left <= start and end <= right for _, left, right in matches):
                matches.append((title, start, end))
            offset = start + 1
    return list(dict.fromkeys(title for title, _, _ in matches))


def merge_candidates(vector_hits, title_hits, ingredient_hits=()):
    rows = {}
    for channel, hits in (('vector', vector_hits), ('title', title_hits), ('ingredient', ingredient_hits)):
        for hit in hits:
            key = hit['chunk_id']
            if key not in rows:
                rows[key] = {**hit, 'retrieval_channels': []}
            if channel not in rows[key]['retrieval_channels']:
                rows[key]['retrieval_channels'].append(channel)
    return sorted(rows.values(), key=lambda row: row['distance'], reverse=True)


def ingredient_candidates(question, chunks):
    """只读取明确列举的首句食材，不把后续否定信息当作已有食材。"""
    import re
    match = re.search(r'我(?:只有|有)\s*([^，。；！？\n]+)', question)
    if not match:
        from ingredient_recommendation import parse_inventory
        inventory=parse_inventory(question)
        if inventory:
            return ingredient_candidates('我有'+'和'.join(inventory)+'。',chunks)
        return [], []
    inventory = match.group(1)
    if re.search(r'没有|不含|除了|但|能做|可以|推荐', inventory):
        return [], []
    terms = [term.strip() for term in re.split(r'、|和|及|与|,', inventory) if term.strip()]
    # 复杂数量、泛指或否定语句不猜测；交给语义检索。
    if not terms or any(not re.fullmatch(r'[\u4e00-\u9fff]{1,10}', term) for term in terms):
        return [], []
    terms = list(dict.fromkeys(terms))
    scores = {}
    required_counts = {}
    for row in chunks:
        if '必备原料' not in row['section']:
            continue
        lines = [line for line in row['content'].splitlines() if re.match(r'^\s*[-*]\s+',line)]
        from ingredient_recommendation import ALIASES
        def canonical(text):
            for old,new in ALIASES.items():
                if len(old)>1:text=text.replace(old,new)
            return text
        matched = {term for term in terms if any(canonical(term) in canonical(line) and not re.search(r'可选|非必[需须]|不必',line) for line in lines)}
        if matched:
            required_counts[row['source']] = required_counts.get(row['source'], 0) + sum(not re.search(r'可选|非必[需须]|不必',line) for line in lines)
            current = scores.setdefault(row['source'], set())
            current.update(matched)
    if not scores:
        return terms, []
    best = max(len(items) for items in scores.values())
    # 并列时优先原料条目较少的菜谱，再按来源路径保持稳定顺序；这只是补候选，不能代表能直接做。
    sources = sorted((source for source, items in scores.items() if len(items) == best), key=lambda source: (required_counts[source], source))[:3]
    return terms, sources
