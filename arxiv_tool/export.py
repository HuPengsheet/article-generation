"""Portable collection artifacts consumed by people or any downstream tool."""

import json
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_all(output_dir, date, papers, filtered, selected, *, reviewed=None, config=None):
    directory = Path(output_dir) / date
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "papers.json", papers)
    write_json(directory / "filtered.json", filtered)
    write_json(directory / "ai-reviewed.json", reviewed if reviewed is not None else filtered)
    write_json(directory / "selected.json", selected)
    if config is not None:
        write_json(directory / "filter-config.json", config)
    lines = [f"# {date} 论文候选", "", "未开启 AI 或模型未返回译名时，中文标题标记为“未翻译”。", ""]
    for record in selected:
        paper = record["paper"]
        ai = record.get("ai") or {}
        title = paper["title"].replace("\n", " ")
        lines.extend(
            [
                f"## {title}",
                "",
                f"中文：{ai.get('title_zh') or paper.get('title_zh') or '未翻译'}",
                "",
                f"链接：{paper['url']}",
                "",
            ]
        )
    (directory / "selected.md").write_text("\n".join(lines), encoding="utf-8")
    return directory
