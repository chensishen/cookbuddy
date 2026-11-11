"""中文字符/双字与英文词 BM25；RRF 融合，不相加异构模型分数。"""
from collections import Counter, defaultdict
from functools import lru_cache
import math
import re


def tokens(text):
    words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.lower())
    result = []
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            result.extend(word)
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            result.append(word)
    return result


class BM25:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.lengths, self.postings = [], defaultdict(list)
        for i, row in enumerate(self.chunks):
            text = (row['title']+' ')*3 + row['section']+' '+row['content']
            counts = Counter(tokens(text))
            self.lengths.append(sum(counts.values()))
            for token, count in counts.items():
                self.postings[token].append((i,count))
        self.average = sum(self.lengths)/max(1,len(self.lengths))

    def search(self, question, k=20):
        scores = defaultdict(float)
        n = len(self.chunks)
        for word in set(tokens(question)):
            hits = self.postings.get(word,())
            idf = math.log(1+(n-len(hits)+.5)/(len(hits)+.5))
            for i, freq in hits:
                norm = 1.2*(1-.75+.75*self.lengths[i]/max(1,self.average))
                scores[i] += idf*freq*2.2/(freq+norm)
        ranked = sorted(scores,key=lambda i:(-scores[i],self.chunks[i]['chunk_id']))[:k]
        return [{**self.chunks[i], 'bm25_score':scores[i]} for i in ranked]


def rrf_merge(channels, constant=60):
    if constant <= 0:
        raise ValueError('RRF 常数必须为正')
    rows, scores = {}, defaultdict(float)
    for name, hits in channels.items():
        seen = set()
        for rank, hit in enumerate(hits,1):
            ident = hit['chunk_id']
            if ident in seen:
                continue
            seen.add(ident)
            if ident not in rows:
                rows[ident] = {**hit,'retrieval_channels':[]}
            rows[ident]['retrieval_channels'].append(name)
            scores[ident] += 1/(constant+rank)
            if 'bm25_score' in hit:
                rows[ident]['bm25_score'] = hit['bm25_score']
    return [{**rows[i],'fusion_score':scores[i]} for i in sorted(rows,key=lambda i:(-scores[i],i))]
