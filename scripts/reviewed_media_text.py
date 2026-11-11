"""Load assistant-checked image words for image lookup only."""
import json

from process_recipe_media import ROOT

FOLDER = ROOT / 'data/processed/media_ocr_pilot'


def load_reviewed_texts():
    pilot = json.loads((FOLDER / 'results.json').read_text())
    review = json.loads((FOLDER / 'reviewed-text.json').read_text())
    records = {r['image_path']: r for r in pilot['records']}
    selected = {}
    for entry in review['entries']:
        path = entry['image_path']
        if path in selected or path not in records:
            raise ValueError('图片复核记录重复或没有OCR来源')
        record = records[path]
        if entry['image_sha256'] != record['image_sha256']:
            raise ValueError('图片复核哈希与OCR来源不一致')
        observed = {r['text'] for r in record['ocr']['observations']}
        chosen = []
        for item in entry['texts']:
            word = item['text']
            if (word not in observed or not word.strip() or any(c.isdigit() for c in word)
                    or item['kind'] not in ('handwritten_label', 'image_subtitle')
                    or any(row['text'] == word for row in chosen)):
                raise ValueError('核对文字不在OCR结果中、含数字或类型不允许')
            chosen.append({'text': word, 'kind': item['kind'],
                           'evidence_type': 'reviewed_image_text',
                           'usage': 'image_lookup_only'})
        selected[path] = {'image_sha256': entry['image_sha256'], 'texts': chosen}
    return selected
