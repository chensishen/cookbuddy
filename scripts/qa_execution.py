"""问答执行策略：不降低核验门槛，复核并行继承同一请求预算。"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import math
import os
import time

from service_runtime import remaining


def execution_profile():
    profile = os.environ.get('RECIPE_QA_PROFILE', 'balanced')
    if profile not in ('serial', 'balanced'):
        raise ValueError('未知问答执行策略')
    return profile


def complete_named_context(named_source, references, chunks):
    if not named_source or not references:
        return False
    expected = {row['chunk_id'] for row in chunks if row['source'] == named_source}
    actual = {row['chunk_id'] for row in references if row['source'] == named_source}
    return bool(expected) and actual == expected and all(
        row['source'] == named_source for row in references)


def model_deadline(reserve_seconds=10):
    if not math.isfinite(reserve_seconds) or not 0 <= reserve_seconds <= 30:
        raise ValueError('原文降级预留时间须为0到30秒')
    left = remaining()
    # 很短的用户预算不再额外扣除；剩余时间不足时依靠总deadline拒绝迟到结果。
    reserve = reserve_seconds if left > reserve_seconds + 5 else 0
    return time.time() + min(40, left - reserve)


def independent_reviews(support, completeness, parallel=False):
    if not parallel:
        result = support()
        return result, completeness() if result['verdict'] in ('supported', 'insufficient') else None
    # 每个线程拥有独立的Context副本；取消与deadline不会在线程边界丢失。
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='recipe-review') as executor:
        first = executor.submit(copy_context().run, support)
        second = executor.submit(copy_context().run, completeness)
        return first.result(), second.result()
