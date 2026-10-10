"""Evidence-checked relevance filtering without web or database dependencies."""

import json
from pathlib import Path

from shared.llm import validate_filter

ROOT = Path(__file__).resolve().parents[1]


def filter_one(paper, llm, prompt):
    result = validate_filter(
        llm.complete(prompt, json.dumps(paper, ensure_ascii=False), True), paper
    )
    result["model"] = llm.model
    if "title_zh" in result and not isinstance(result["title_zh"], str):
        raise ValueError("AI 中文标题格式无效。")
    return result


def run(records, config, llm, progress=lambda message: None):
    prompt = (ROOT / config["ai_filter"]["prompt_file"]).read_text(encoding="utf-8")
    results = []
    for index, record in enumerate(records, 1):
        item = dict(record)
        progress(f"AI 筛选 {index}/{len(records)}：{item['paper']['title'][:65]}")
        try:
            item["ai"] = filter_one(item["paper"], llm, prompt)
            item["ai_error"] = None
        except (ValueError, RuntimeError) as error:
            item["ai_error"] = str(error)
            item["ai"] = None
        results.append(item)
    return results
