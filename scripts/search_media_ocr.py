"""Inspect OCR candidates by literal text; never return them as verified recipe facts."""
import argparse
import json

from pilot_media_ocr import OUT, checked_asset


def search(query, records, title=''):
    if not query.strip() or len(query) > 200:
        raise ValueError('检索词不能为空或超过200字')
    matches = []
    for row in records:
        if title and not any(ref['title'] == title for ref in row['references']):
            continue
        hits = [obs for obs in row['ocr']['observations'] if query.casefold() in obs['text'].casefold()]
        if hits:
            checked_asset({'path': row['image_path'], 'sha256': row['image_sha256'],
                           'references': row['references']})
            matches.append({'image_path': row['image_path'], 'image_sha256': row['image_sha256'],
                            'hits': hits, 'references': row['references'],
                            'evidence_type': 'image_ocr_candidate',
                            'status': 'needs_visual_review',
                            'enabled_for_recipe_answers': False})
    return matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('query')
    parser.add_argument('--title', default='')
    args = parser.parse_args()
    data = json.loads((OUT / 'results.json').read_text())
    found = search(args.query, data['records'], args.title)
    print(json.dumps({'query': args.query, 'matches': found,
                      'notice': '仅检索图片OCR候选；请查看原图核对文字和数量，结果不进入菜谱问答。'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
