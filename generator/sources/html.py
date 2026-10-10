"""Extract static article content, preserving structural source locations."""

import re
from pathlib import Path

from readability import Document

from generator.figures.html_img import _Document, _Node
from generator.figures.local import acquire_image
from generator.sources.base import ExtractedText, Material, Source
from shared.http import decode_text

SKIP = {
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "noscript",
    "form",
    "button",
    "template",
}
BLOCKS = {"p", "li", "pre", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "figcaption"}


def visible_text(node):
    if isinstance(node, str):
        return node
    if node.tag in SKIP or "hidden" in node.attrs or node.attrs.get("aria-hidden") == "true":
        return ""
    if node.tag == "math" and node.attrs.get("alttext"):
        return node.attrs["alttext"]
    if node.tag == "br":
        return "\n"
    text = "".join(visible_text(child) for child in node.children)
    return text + (
        "\n"
        if node.tag in BLOCKS or node.tag in ("div", "section", "tr")
        else "\t"
        if node.tag in ("td", "th")
        else ""
    )


class HtmlSource(Source):
    def extract(self, path_or_url, work_dir, *, base_url=None, encoding=None):
        path, work_dir = Path(path_or_url), Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        markup = decode_text(path.read_bytes(), encoding)
        document = _Document()
        document.feed(markup)
        nodes = list(document.root.walk())
        preferred = [n for n in nodes if n.tag in ("article", "main")]
        body = (
            max(preferred, key=lambda n: len(visible_text(n)))
            if preferred
            else next((n for n in nodes if n.tag == "body"), document.root)
        )
        title = next((n.text().strip() for n in nodes if n.tag == "title"), path.stem)
        authors = [
            n.attrs.get("content", "")
            for n in nodes
            if n.tag == "meta" and n.attrs.get("name", "").lower() in ("author", "citation_author")
        ]
        sections, figures, limitations = [], [], []
        if not preferred:
            try:
                readable = _Document()
                readable.feed(Document(markup).summary(html_partial=True))
                if visible_text(readable.root).strip():
                    body = readable.root
                    limitations.append(
                        "页面没有 article/main，使用 readability 估计正文；请核对正文范围。"
                    )
            except Exception as error:
                limitations.append(
                    f"readability 未成功提取正文（{type(error).__name__}），使用去除导航后的页面。"
                )
        heading = ""

        def visit(node):
            nonlocal heading, title
            if (
                node.tag in SKIP
                or "hidden" in node.attrs
                or node.attrs.get("aria-hidden") == "true"
            ):
                return
            if node.tag in BLOCKS:
                raw = visible_text(node).strip()
                text = raw if node.tag in ("pre", "table") else re.sub(r"\s+", " ", raw)
                if text:
                    if node.tag.startswith("h") and node.tag[1:].isdigit():
                        heading = text
                        if node.tag == "h1":
                            title = text
                    sections.append(ExtractedText(f"paragraph {len(sections) + 1}", text, heading))
                # Images within paragraphs/tables still need extraction.
                for child in node.walk():
                    if child.tag == "img":
                        image(child)
                return
            if node.tag == "img":
                image(node)
            for child in node.children:
                if isinstance(child, _Node):
                    visit(child)

        def image(node):
            reference = node.attrs.get("src") or node.attrs.get("data-src")
            caption = node.attrs.get("alt", "")
            parent = node.parent
            while parent is not None and parent is not body:
                if parent.tag == "figure":
                    caption = next(
                        (n.text().strip() for n in parent.walk() if n.tag == "figcaption"), caption
                    )
                    break
                parent = parent.parent
            try:
                figures.append(
                    acquire_image(
                        reference,
                        base_url or str(path.resolve()),
                        work_dir,
                        len(figures) + 1,
                        caption,
                        "html",
                    )
                )
            except (ValueError, RuntimeError, OSError) as error:
                limitations.append(f"图片未提取（{type(error).__name__}）：{reference}")

        visit(body)
        if not sections and visible_text(body).strip():
            sections = [ExtractedText("paragraph 1", visible_text(body).strip())]
        if not sections:
            raise ValueError("HTML 未提取到正文，可能需要登录或 JavaScript 渲染。")

        limitations.append("仅解析静态 HTML，不执行 JavaScript，不绕过登录或付费访问。")
        (work_dir / "source.html").write_bytes(path.read_bytes())
        return Material(
            title,
            authors,
            sections,
            figures,
            {
                "source_type": "html",
                "url": base_url,
                "path": str(path.resolve()),
                "work_dir": str(work_dir.resolve()),
            },
            limitations,
        )
