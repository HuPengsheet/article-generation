"""Generate from files, URLs, or portable JSON source lists, without a database."""

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from generator import sources
from generator.editorial import validate_team_context
from generator.pipeline import generate
from shared.llm import LLM

ROOT = Path(__file__).resolve().parents[1]


def expand_sources(values):
    expanded = []
    for value in values:
        if urlsplit(value).scheme in ("http", "https") or Path(value).suffix.lower() != ".json":
            expanded.append(value)
            continue
        path = Path(value).resolve()
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("材料 JSON 必须为列表，元素是文件路径、链接或含 paper/url 的对象。")
        for record in records:
            if isinstance(record, str):
                source = record
            elif isinstance(record, dict):
                if record.get("ai") is not None and not isinstance(record["ai"], dict):
                    raise ValueError("材料 JSON 的 ai 字段必须是对象。")
                if (
                    record.get("review") in ("skipped", "later")
                    or (record.get("ai") or {}).get("decision") == "reject"
                ):
                    continue
                paper = record.get("paper", record)
                source = paper.get("url") or paper.get("path") if isinstance(paper, dict) else None
            else:
                source = None
            if not isinstance(source, str) or not source.strip():
                raise ValueError("材料 JSON 记录缺少有效 url/path。")
            if urlsplit(source).scheme not in ("http", "https"):
                source = str((path.parent / source).resolve())
            expanded.append(source)
    if not expanded:
        raise ValueError("没有可生成的材料。")
    return expanded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sources", nargs="+", help="PDF/HTML/Markdown 文件、HTTP(S) 链接或 JSON 列表"
    )
    parser.add_argument("--angle", default="")
    parser.add_argument("--audience", default="工程师")
    parser.add_argument("--depth", choices=["brief", "detailed", "deep-dive"], default="detailed")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/articles")
    parser.add_argument("--no-summary", action="store_true")
    parser.add_argument("--team-context", type=Path)
    parser.add_argument(
        "--batch", action="store_true", help="每个展开后的来源分别生成一篇；默认合成一篇"
    )
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    try:
        values = expand_sources(args.sources)
        model = LLM()
        if not model.configured:
            raise ValueError("请在 .env 配置模型服务后生成文章。")
        context = (
            validate_team_context(json.loads(args.team_context.read_text(encoding="utf-8")))
            if args.team_context
            else None
        )
        # Every invocation owns a fresh directory; no accidental overwrite of existing work.
        directory = args.output_dir / (
            datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        groups = [[value] for value in values] if args.batch else [values]
        failures = []
        for index, group in enumerate(groups, 1):
            work_dir = directory / f"article-{index:03d}" if args.batch else directory
            try:
                materials = []
                for number, value in enumerate(group, 1):
                    print(f"提取材料 {number}/{len(group)}：{value}")
                    materials.append(
                        sources.detect(value).extract(value, work_dir / "sources" / f"S{number}")
                    )
                result = generate(
                    materials,
                    model,
                    work_dir,
                    angle=args.angle,
                    audience=args.audience,
                    depth=args.depth,
                    include_summary=not args.no_summary,
                    team_context=context,
                    progress=print,
                )
                print(f"已生成：{result.directory}/article.md")
            except (ValueError, RuntimeError, OSError) as error:
                if not args.batch:
                    raise
                failures.append({"sources": group, "error": str(error)})
                print(f"第 {index} 篇失败：{error}")
        if failures:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "failures.json").write_text(
                json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 1 if failures else 0
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"失败：{error}\n")
