"""Run a bounded, provenance-checked OCR pilot; results are review candidates only."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from process_recipe_media import ROOT, check_ocr, local_file, save

INDEX = ROOT / 'data/processed/media_pilot/asset-index.json'
OUT = ROOT / 'data/processed/media_ocr_pilot'
DEFAULT_IMAGES = (
    'data/HowToCook/dishes/dessert/戚风蛋糕/IMG_1516.jpg',
    'data/HowToCook/dishes/staple/基础牛奶面包/2-2设备简介2.jpg',
    'data/HowToCook/dishes/dessert/戚风蛋糕/DSC08621.jpg',
    'data/HowToCook/dishes/aquatic/清蒸鲈鱼/改刀.jpg',
    'data/HowToCook/dishes/aquatic/白灼虾/白灼虾.webp',
    'data/HowToCook/dishes/condiment/草莓酱/做好的草莓酱.png',
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_asset(asset):
    path = local_file(asset['path'])
    if sha(path) != asset['sha256']:
        raise ValueError('图片与来源索引的哈希不一致：' + asset['path'])
    for ref in asset['references']:
        source = local_file(ref['source'])
        lines = source.read_text().splitlines()
        if (sha(source) != ref['source_sha256'] or
                not 1 <= ref['source_line'] <= len(lines) or
                lines[ref['source_line'] - 1] != ref['source_quote']):
            raise ValueError('菜谱原文与图片引用不一致：' + ref['source'])
    return path


def run_ocr(asset, cache_dir=OUT / 'cache'):
    path = checked_asset(asset)
    engine_hash = sha(ROOT / 'scripts/image_ocr.swift')
    cache = cache_dir / (asset['sha256'] + '.json')
    if cache.is_file():
        data = json.loads(cache.read_text())
        if (data.get('image_sha256') == asset['sha256'] and
                data.get('engine_source_sha256') == engine_hash):
            check_ocr(data['result'])
            return data['result'], True
    response = subprocess.run(['swift', str(ROOT / 'scripts/image_ocr.swift'), str(path)],
                              capture_output=True, text=True, timeout=180)
    if response.returncode:
        raise RuntimeError('OCR运行失败：' + response.stderr[-500:])
    result = json.loads(response.stdout)
    check_ocr(result)
    cache_dir.mkdir(parents=True, exist_ok=True)
    save(cache, {'image_sha256': asset['sha256'],
                 'engine_source_sha256': engine_hash, 'result': result})
    return result, False


def candidate(asset, result):
    return {'image_path': asset['path'], 'image_sha256': asset['sha256'],
            'references': asset['references'], 'ocr': result,
            'review_status': 'needs_visual_review' if result['observations'] else 'no_text_detected',
            'enabled_for_recipe_answers': False,
            'evidence_type': 'image_ocr_candidate',
            'notice': 'OCR文字来自图片，不是Markdown原文；数字、单位和配方关系须对照原图人工核对。'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', action='append', help='从正式来源索引选择图片，可多次传入')
    args = parser.parse_args()
    index = json.loads(INDEX.read_text())
    assets = {a['path']: a for a in index['assets']}
    selected = args.image or DEFAULT_IMAGES
    if len(selected) != len(set(selected)) or len(selected) > 20:
        raise ValueError('一次最多20张且不能重复')
    if any(path not in assets for path in selected):
        raise ValueError('图片必须在正式来源索引中')
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    failures = []
    cache_hits = 0
    for name in selected:
        try:
            result, hit = run_ocr(assets[name])
            cache_hits += int(hit)
            records.append(candidate(assets[name], result))
            print(name + ': ' + str(len(result['observations'])) + ' OCR区域', flush=True)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            failures.append({'image_path': name, 'error': str(exc)})
    summary = {'created_at': datetime.now(timezone.utc).isoformat(),
               'selected': len(selected), 'processed': len(records), 'failed': len(failures),
               'cache_hits': cache_hits,
               'images_with_text': sum(bool(r['ocr']['observations']) for r in records),
               'ocr_regions': sum(len(r['ocr']['observations']) for r in records),
               'enabled_for_recipe_answers': 0,
               'scope': '有限图片OCR候选扫描；未进行语义/数字人工验收。'}
    output = OUT / ('results-custom.json' if args.image else 'results.json')
    save(output, {'summary': summary, 'records': records, 'failures': failures})
    print(json.dumps(summary, ensure_ascii=False))
    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
