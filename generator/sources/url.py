"""Fetch a URL, sniff content, then delegate; arXiv landing pages resolve to full text."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from generator.figures.html_img import _Document
from generator.figures.latex import collect_figures
from generator.sources.base import Figure, Source
from generator.sources.html import HtmlSource
from generator.sources.markdown import MarkdownSource
from generator.sources.pdf import PdfSource
from shared.http import decode_text, fetch


def arxiv_identifier(url):
    parsed = urlsplit(url)
    if parsed.hostname not in ("arxiv.org", "www.arxiv.org"):
        return None
    match = re.fullmatch(
        r"/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?)(?:\.pdf)?/?", parsed.path
    )
    return match[1] if match else None


class UrlSource(Source):
    def extract(self, path_or_url, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        identifier = arxiv_identifier(path_or_url)
        if identifier:
            return self.extract_arxiv(identifier, work_dir)
        result = fetch(path_or_url)
        raw, mime = result["content"], result["content_type"].split(";")[0].strip().lower()
        suffix = Path(urlsplit(result["url"]).path).suffix.lower()
        if raw.startswith(b"%PDF-"):
            path = work_dir / "download.pdf"
            path.write_bytes(raw)
            material = PdfSource().extract(str(path), work_dir)
        elif mime == "application/pdf":
            raise ValueError("链接声明为 PDF，但内容不是有效 PDF。")
        else:
            text = decode_text(raw, result.get("encoding"))
            html = bool(
                re.search(r"<(?:!doctype\s+html|html|head|body|article|main)\b", text[:8192], re.I)
            )
            if mime in ("text/html", "application/xhtml+xml") or html:
                path = work_dir / "download.html"
                path.write_bytes(raw)
                material = HtmlSource().extract(
                    str(path), work_dir, base_url=result["url"], encoding=result.get("encoding")
                )
            elif mime in ("text/markdown", "text/x-markdown") or suffix in (".md", ".markdown"):
                path = work_dir / "download.md"
                path.write_text(text, encoding="utf-8")
                material = MarkdownSource().extract(str(path), work_dir, base_url=result["url"])
            else:
                raise ValueError("链接未返回支持的 PDF、HTML 或 Markdown；不把未知响应当成正文。")
        material.metadata.update(url=result["url"], requested_url=path_or_url)
        return material

    def extract_arxiv(self, identifier, work_dir):
        title, authors = "", []
        # An explicit version can proceed even if the metadata endpoint is unavailable.
        try:
            response = fetch("https://arxiv.org/abs/" + identifier, limit=15 * 1024 * 1024)
            text = response["content"].decode("utf-8", errors="replace")
            document = _Document()
            document.feed(text)
            meta = [n for n in document.root.walk() if n.tag == "meta"]
            title = next(
                (
                    n.attrs.get("content", "")
                    for n in meta
                    if n.attrs.get("name") == "citation_title"
                ),
                "",
            )
            authors = [
                n.attrs.get("content", "") for n in meta if n.attrs.get("name") == "citation_author"
            ]
            if not re.search(r"v\d+$", identifier):
                versions = re.findall(re.escape(identifier) + r"v(\d+)\b", text)
                if not versions:
                    raise ValueError("无法确认 arXiv 全文版本，请提供带 vN 的链接。")
                identifier += "v" + str(max(map(int, versions)))
        except (ValueError, RuntimeError):
            if not re.search(r"v\d+$", identifier):
                raise ValueError("无法确认 arXiv 全文版本，请提供带 vN 的链接。") from None
        paper = {
            "version_id": identifier,
            "pdf_url": "https://arxiv.org/pdf/" + identifier,
            "url": "https://arxiv.org/abs/" + identifier,
            "title": title,
            "authors": authors,
        }
        return self.extract_paper(paper, work_dir)

    def extract_paper(self, paper, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        identifier = paper["version_id"]
        title, authors = paper.get("title", ""), paper.get("authors", [])
        if isinstance(authors, str):
            authors = [authors] if authors else []
        pdf_url = paper["pdf_url"]
        raw = fetch(pdf_url)["content"]
        path = work_dir / "download.pdf"
        path.write_bytes(raw)
        material = PdfSource().extract(str(path), work_dir)
        paper = {
            "version_id": identifier,
            "pdf_url": pdf_url,
            "url": "https://arxiv.org/abs/" + identifier,
            "title": title or material.title,
            "authors": authors,
        }
        try:
            extracted, limitations = collect_figures(paper, work_dir)
            if extracted:
                material.figures = [
                    Figure(
                        item["file"],
                        item["caption_en"],
                        item["order"],
                        item.get("origin", "latex"),
                        item.get("panels", []),
                        {
                            k: v
                            for k, v in item.items()
                            if k not in ("file", "caption_en", "order", "origin", "panels")
                        },
                    )
                    for item in extracted
                ]
            material.limitations.extend(limitations)
        except Exception as error:
            material.limitations.append(
                f"arXiv 源码/HTML 配图失败（{type(error).__name__}），保留 PDF 解析结果。"
            )
        material.title, material.authors = paper["title"], authors or material.authors
        material.metadata.update(
            url=paper["url"], version_id=identifier, paper=paper, source_type="arxiv"
        )
        return material
