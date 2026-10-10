"""Move legacy report-backed articles into the canonical article asset directory."""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from generator.identifiers import safe_id
from generator.writer import article_images


def migrate(data_dir, reports_dir):
    data_dir, reports_dir = Path(data_dir).resolve(), Path(reports_dir).resolve()
    count = 0
    with sqlite3.connect(data_dir / "app.sqlite3") as db:
        db.execute("BEGIN IMMEDIATE")
        for batch, paper_id, raw in db.execute("SELECT batch,id,data FROM papers").fetchall():
            state = json.loads(raw)
            article = state.get("article")
            if not article or not any(
                k in article for k in ("notes_url", "images_url", "image_base_url")
            ):
                continue
            base = article.get("image_base_url", "")
            if not base.startswith("/reports/"):
                raise ValueError("旧文章没有可确认的本地报告目录。")
            source = (reports_dir / base.removeprefix("/reports/")).resolve()
            if not source.is_relative_to(reports_dir) or not source.is_dir():
                raise ValueError("旧文章报告目录无效。")
            target = (
                data_dir / "articles" / batch / safe_id(state["paper"]["version_id"])
            ).resolve()
            if not target.is_relative_to(data_dir / "articles"):
                raise ValueError("旧文章目标目录无效。")
            paths = {}
            for file in article_images(article["markdown"]):
                origin = (source / file).resolve()
                if (
                    not origin.is_relative_to(source)
                    or not origin.is_file()
                    or origin.suffix.lower() != ".png"
                ):
                    raise ValueError("旧文章配图路径无效。")
                destination = "figures/" + origin.name
                if destination in paths.values() and file not in paths:
                    raise ValueError("旧文章配图文件名重复。")
                paths[file] = destination
            (target / "figures").mkdir(parents=True, exist_ok=True)
            for old, new in paths.items():
                shutil.copyfile(source / old, target / new)
                article["markdown"] = article["markdown"].replace(
                    "](" + old + ")", "](" + new + ")"
                )
            if any(file not in paths.values() for file in article_images(article["markdown"])):
                raise ValueError("旧文章含不支持迁移的图片引用。")
            for figure in article.get("notes", {}).get("figures", []):
                if figure.get("file") in paths:
                    figure["file"] = paths[figure["file"]]
            for key in ("notes_url", "images_url", "image_base_url"):
                article.pop(key, None)
            article["directory"] = str(target)
            (target / "article.md").write_text(article["markdown"], encoding="utf-8")
            (target / "notes.json").write_text(
                json.dumps(article.get("notes", {}), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            review = next(
                (
                    source / name
                    for name in ("review.md", "paper-notes.md")
                    if (source / name).is_file()
                ),
                None,
            )
            if review:
                shutil.copyfile(review, target / "review.md")
            db.execute(
                "UPDATE papers SET data=? WHERE batch=? AND id=?",
                (json.dumps(state, ensure_ascii=False), batch, paper_id),
            )
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args()
    print(f"Migrated {migrate(args.data_dir, args.reports_dir)} article(s).")
