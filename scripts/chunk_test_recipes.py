"""按 Markdown 副标题切分测试菜谱，保留正文及来源信息。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/HowToCook/test_dishes"
OUTPUT = ROOT / "data/processed/test_dishes"


def split_recipe(path):
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    title = path.stem
    section = "简介"
    hierarchy = {}
    body = []
    start_line = 1
    chunks = []
    fence = None

    def flush(end_line):
        content = "".join(body).strip()
        if not content:
            return
        chunks.append({
            "chunk_id": f"{path.relative_to(SOURCE).as_posix()}::{len(chunks) + 1:03d}",
            "title": title,
            "section": section,
            "source": path.relative_to(ROOT).as_posix(),
            "source_start_line": start_line,
            "source_end_line": end_line,
            "content": content,
            "text": f"菜名：{title}\n章节：{section}\n原文路径：{path.relative_to(ROOT).as_posix()}\n\n{content}",
        })

    for number, line in enumerate(lines, 1):
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            body.append(line)
            continue
        heading = None if fence else re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if not heading:
            body.append(line)
            continue
        level = len(heading.group(1))
        label = re.sub(r"\s+#+\s*$", "", heading.group(2))
        if level == 1:
            title = label.removesuffix("的做法")
            continue
        flush(number - 1)
        hierarchy = {depth: name for depth, name in hierarchy.items() if depth < level}
        hierarchy[level] = label
        section = " > ".join(hierarchy.values())
        body = [line]
        start_line = number
        # 标题本身不构成有效正文；在 flush 时过滤空父章节。
    flush(len(lines))
    return [chunk for chunk in chunks if any(
        line.strip() and not re.match(r"^ {0,3}#{2,6}\s+", line)
        for line in chunk["content"].splitlines()
    )]


def main():
    chunks = []
    for path in sorted(SOURCE.rglob("*.md")):
        recipe_chunks = split_recipe(path)
        # 过滤空父章节后重新连续编号。
        for index, chunk in enumerate(recipe_chunks, 1):
            chunk["chunk_id"] = f"{path.relative_to(SOURCE).as_posix()}::{index:03d}"
        chunks.extend(recipe_chunks)
        print(f"{path.stem}：{len(recipe_chunks)} 块")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "chunks.jsonl").write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks), encoding="utf-8"
    )
    preview = ["# 测试菜谱分块预览\n\n按 Markdown 副标题切分；保留简介及章节层级。图片仅保留原始 Markdown 引用，未解析图片内容。\n"]
    for index, chunk in enumerate(chunks, 1):
        preview.append(
            f"\n## 块 {index}：{chunk['title']} / {chunk['section']}\n\n"
            f"- 原文路径：`{chunk['source']}`\n"
            f"- 原文行号：{chunk['source_start_line']}–{chunk['source_end_line']}\n"
            f"- 正文字符数：{len(chunk['content'])}\n\n"
            f"````markdown\n{chunk['text']}\n````\n"
        )
    (OUTPUT / "chunks-preview.md").write_text("".join(preview), encoding="utf-8")
    print(f"总计：{len(chunks)} 块；输出目录：{OUTPUT}")


if __name__ == "__main__":
    main()
