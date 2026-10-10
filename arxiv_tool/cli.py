"""Collect one complete announcement day, filter, optionally select, and export."""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from arxiv_tool import ai_filter, collector, export, keyword_filter
from shared.llm import LLM

ROOT = Path(__file__).resolve().parents[1]


def interactive_select(records):
    for record in records:
        paper = record["paper"]
        while True:
            answer = (
                input(f"\n{paper['title']}\n{paper['url']}\n选择 [y] / 跳过 [n] / 稍后 [l]：")
                .strip()
                .lower()
            )
            if not answer and record.get("review") in ("selected", "skipped", "later"):
                break
            if answer in ("y", "n", "l"):
                record["review"] = {"y": "selected", "n": "skipped", "l": "later"}[answer]
                break
    return [record for record in records if record["review"] == "selected"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/ai_infra.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/arxiv")
    parser.add_argument("--skip-ai", action="store_true", help="无需模型服务，仅做关键词筛选")
    parser.add_argument("--interactive", action="store_true", help="进入人工交互选择")
    parser.add_argument("--date", help="指定公告日；历史日期仅能读取已有缓存")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        llm = None if args.skip_ai else LLM()
        if not args.skip_ai and not llm.configured:
            raise ValueError("AI 筛选需要模型配置；仅采集和关键词筛选请加 --skip-ai。")
        date, papers, _ = collector.collect(args.output_dir, print, requested_date=args.date)
        filtered = keyword_filter.run(papers, config)
        passed = [record for record in filtered if record["keyword_filter"]["passed"]]
        reviewed = passed if args.skip_ai else ai_filter.run(passed, config, llm, print)
        candidates = [
            dict(record, review="pending")
            for record in reviewed
            if args.skip_ai or (record.get("ai") or {}).get("decision") in ("keep", "uncertain")
        ]
        previous_file = args.output_dir / date / "human-review.json"
        if previous_file.is_file():
            previous = {
                item["paper"]["id"]: item.get("review", "pending")
                for item in json.loads(previous_file.read_text(encoding="utf-8"))
            }
            for record in candidates:
                status = previous.get(record["paper"]["id"], "pending")
                if status in ("pending", "selected", "skipped", "later"):
                    record["review"] = status
        selected = (
            interactive_select(candidates)
            if args.interactive
            else [record for record in candidates if record["review"] not in ("skipped", "later")]
        )
        directory = export.save_all(
            args.output_dir, date, papers, filtered, selected, reviewed=reviewed, config=config
        )
        export.write_json(directory / "human-review.json", candidates)
        errors = sum(bool(item.get("ai_error")) for item in reviewed)
        print(
            f"完成：全量 {len(papers)}，关键词通过 {len(passed)}，候选/选中 {len(selected)}；输出 {directory}"
        )
        if errors:
            print(f"AI 筛选失败 {errors} 篇，详见 ai-reviewed.json；失败项未作为已筛选结果。")
        return 1 if errors else 0
    except (ValueError, RuntimeError, OSError, EOFError) as error:
        parser.exit(1, f"失败：{error}\n")
