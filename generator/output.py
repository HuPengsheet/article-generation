"""Publish clean Markdown and portable, self-contained browser preview assets."""

import base64
import json
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from generator.evidence import publication_copy
from generator.writer import ASPECTS, article_images
from shared.wechat import preview


@dataclass
class ArticleResult:
    markdown: str
    notes: dict
    directory: str
    model: str

    def as_dict(self):
        return {
            "markdown": self.markdown,
            "notes": self.notes,
            "directory": self.directory,
            "model": self.model,
        }


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def review_markdown(evidence):
    lines = [
        "# 核对材料与写作自查",
        "",
        "引文已匹配来源正文；引用位置校验不保证解读正确，实验未独立复现。",
        "",
        "## 材料来源",
        "",
    ]
    for source in evidence["sources"]:
        metadata = source["metadata"]
        lines.append(
            f"- {source['id']}：{source['title']}；{metadata.get('url') or metadata.get('path', '')}"
        )
    locations = {block["ref"]: block for block in evidence["sections"]}
    lines.extend(["", "## 核对依据", ""])
    for fact in evidence["facts"]:
        block = locations[fact["ref"]]
        lines.append(
            f"- [{fact['ref']}] {block['location']} / {block['heading']}：{fact['claim']} — 原文：{fact['quote']}"
        )
    lines.extend(["", "## 配图清单", ""])
    for figure in evidence["figures"]:
        lines.append(
            f"- {figure.get('source_id', '自绘')}，{figure['file']}，{figure['origin']}：{figure['caption_en']}"
        )
        for panel in figure.get("panels", []):
            lines.append(
                f"  - {panel.get('label', '')} {panel.get('position', '')}：{panel.get('caption_en', '')}"
            )
        if figure.get("assumptions"):
            lines.append(f"  - 自绘假设：{figure['assumptions']}")
    lines.extend(["", "## 正文段落与来源", ""])
    for citation in evidence["citations"]:
        lines.extend([f"### 原稿段落 {citation['paragraph']}", "", citation["text"], ""])
    lines.extend(["", "## 写作自查", ""])
    review = evidence["review"]
    lines.append(f"改写尝试：{review['rewrite_attempted']}；应用：{review['rewrite_applied']}。")
    for issue in review["lint_before"] + review["issues"] + review["lint_after"]:
        lines.append(f"- {issue['location']}：{issue['problem']}；建议：{issue['suggestion']}")
    critique = review.get("critique")
    if critique:
        lines.extend(["", "## 独立审稿（二轮修改依据）", ""])
        lines.append(
            f"评分：{critique['score']}/10；二轮修改尝试：{review.get('revise_attempted', False)}；"
            f"应用：{review.get('revise_applied', False)}。"
        )
        if review.get("revise_error"):
            lines.append(f"未应用原因：{review['revise_error']}")
        lines.extend(["", critique["verdict"], ""])
        for issue in critique["issues"]:
            lines.append(
                f"- [{ASPECTS[issue['aspect']]}/{issue['severity']}] {issue['location']}："
                f"{issue['problem']}；建议：{issue['suggestion']}"
            )
    for source in (evidence.get("team_context") or {}).get("sources", []):
        lines.append(f"- 团队背景来源：[{source['title']}]({source['url']})")
    lines.extend(["", "## 阅读局限", ""])
    lines.extend("- " + item for item in evidence["limitations"])
    return "\n".join(lines) + "\n"


def _image_path(directory, filename):
    if not isinstance(filename, str) or "\\" in filename or "\x00" in filename:
        raise ValueError(f"导出图片路径无效：{filename!r}")
    relative = Path(filename)
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "figures"
        or relative.suffix.lower() != ".png"
    ):
        raise ValueError(f"导出图片路径无效：{filename}")
    root = directory.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"导出图片路径越界：{filename}")
    if not path.is_file():
        raise ValueError(f"导出图片文件缺失：{filename}；请恢复素材后重试。")
    return path


def save_output(article, evidence, work_dir):
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    used_images = article_images(article)
    if not used_images:
        raise ValueError("正文缺少实际配图，不能交付无图完成稿。")
    allowed = {figure["file"] for figure in evidence["figures"]}
    if any(filename not in allowed for filename in used_images):
        raise ValueError("正文引用了配图清单外图片，已停止导出。")
    images = {}
    for figure in evidence["figures"]:
        filename = figure["file"]
        images[filename] = _image_path(directory, filename)
        try:
            with pymupdf.open(images[filename]) as image:
                image[0].get_pixmap()
        except Exception as error:
            raise ValueError(f"配图无法解码：{filename}，已停止导出。") from error
    image_urls = {
        filename: "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        for filename, path in images.items()
    }
    (directory / "article-cited.md").write_text(article, encoding="utf-8")
    published, citations = publication_copy(article)
    evidence["citations"] = citations
    (directory / "article.md").write_text(published, encoding="utf-8")
    write_json(directory / "notes.json", evidence)
    (directory / "review.md").write_text(review_markdown(evidence), encoding="utf-8")
    # Relative PNG addresses work when served with `python -m http.server`.
    (directory / "wechat.html").write_text(
        preview(
            published,
            download_url="article.md",
            notes_url="review.md",
            images_url="images.zip",
            image_urls=image_urls,
        ),
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory(prefix=".images-", dir=directory) as temporary:
        staged_zip = Path(temporary) / "images.zip"
        with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("article.md", published)
            for filename in images:
                path = _image_path(directory, filename)
                try:
                    archive.write(path, filename)
                except OSError as error:
                    raise ValueError(f"导出图片读取失败：{filename}；请检查文件后重试。") from error
        staged_zip.replace(directory / "images.zip")
    return ArticleResult(published, evidence, str(directory.resolve()), evidence["model"])
