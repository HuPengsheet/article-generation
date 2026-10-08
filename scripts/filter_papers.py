#!/usr/bin/env python3
"""Filter a complete daily JSON paper list without discarding original records."""

import argparse
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[-‐‑‒–—−]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def matches(text, keyword):
    # Word boundaries prevent short names such as MoE from matching longer words.
    return re.search(r"(?<!\w)" + re.escape(normalize(keyword)) + r"(?!\w)", text) is not None


def flatten(groups):
    return [(topic, word) for topic, words in groups.items() for word in words]


def evaluate(paper, config):
    texts = []
    for field in config["fields"]:
        value = paper.get(field, "")
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        if value is not None:
            texts.append(normalize(str(value)))

    def hits(words):
        return [word for word in words if any(matches(text, word) for text in texts)]

    strong = [(topic, word) for topic, word in flatten(config["strong_keywords"]) if hits([word])]
    contextual = [
        (topic, word) for topic, word in flatten(config["contextual_keywords"]) if hits([word])
    ]
    context = hits(config["ai_context_keywords"])
    excluded = hits(config["exclude_keywords"])
    passed = not excluded and bool(strong or (contextual and context))
    if excluded:
        reason = "excluded_keyword"
    elif strong:
        reason = "strong_keyword"
    elif contextual and context:
        reason = "contextual_keyword_with_ai_context"
    elif contextual:
        reason = "missing_ai_context"
    else:
        reason = "no_keyword_match"
    return {
        "passed": passed,
        "reason": reason,
        "strong_hits": [{"topic": topic, "keyword": word} for topic, word in strong],
        "contextual_hits": [{"topic": topic, "keyword": word} for topic, word in contextual],
        "ai_context_hits": context,
        "exclude_hits": excluded,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON array of daily papers")
    parser.add_argument("--config", type=Path, default=ROOT / "config/ai_infra.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    papers = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(papers, list) or any(not isinstance(p, dict) for p in papers):
        parser.error("input must be a JSON array of paper objects")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    results = [{"paper": paper, "keyword_filter": evaluate(paper, config)} for paper in papers]
    passed = sum(result["keyword_filter"]["passed"] for result in results)
    output = {
        "filter_name": config["name"],
        "filter_version": config["version"],
        "config": config,
        "total": len(results),
        "passed": passed,
        "rejected": len(results) - passed,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Total: {len(results)}; passed: {passed}; rejected: {len(results) - passed}")


if __name__ == "__main__":
    main()
