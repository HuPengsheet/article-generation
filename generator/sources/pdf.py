"""PDF extraction independent of arXiv metadata or collection state."""

import shutil
from pathlib import Path

import pymupdf

from generator.pdf_text import extract_pages
from generator.sources.base import ExtractedText, Figure, Material, Source


class PdfSource(Source):
    def extract(self, path_or_url, work_dir):
        path, work_dir = Path(path_or_url), Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        pages = extract_pages(path.read_bytes())
        stored = work_dir / "source.pdf"
        if path.resolve() != stored.resolve():
            shutil.copyfile(path, stored)
        limitations, figures = [], []
        with pymupdf.open(path) as document:
            title = document.metadata.get("title") or path.stem
            authors = [document.metadata["author"]] if document.metadata.get("author") else []
            seen = set()
            for page_no, page in enumerate(document, 1):
                for image in page.get_images(full=True):
                    xref = image[0]
                    if xref in seen or image[2] < 128 or image[3] < 128:
                        continue
                    seen.add(xref)
                    try:
                        pix = pymupdf.Pixmap(document, xref)
                        if pix.width * pix.height > 40_000_000:
                            raise ValueError("图片超过像素限制")
                        if pix.colorspace is None:
                            continue
                        if pix.colorspace.n != 3 or pix.alpha:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        order = len(figures) + 1
                        target = work_dir / "figures" / f"pdf-image-{order:03d}.png"
                        target.parent.mkdir(exist_ok=True)
                        pix.save(target)
                        figures.append(
                            Figure(
                                "figures/" + target.name,
                                f"PDF 第 {page_no} 页嵌入图片；未自动匹配原图注",
                                order,
                                "pdf",
                                metadata={"page": page_no},
                            )
                        )
                    except (ValueError, RuntimeError):
                        limitations.append(f"PDF 第 {page_no} 页有图片未成功提取。")
        limitations.append("PDF 配图仅提取嵌入位图，矢量图与原图注需要人工核对；未执行 OCR。")
        for page in pages:
            if not page["text"]:
                limitations.append(f"PDF 第 {page['page']} 页未提取到文字。")
            limitations.extend(
                f"PDF 第 {page['page']} 页：{warning}" for warning in page["extraction_warnings"]
            )
        return Material(
            title,
            authors,
            [ExtractedText(f"PDF p.{p['page']}", p["text"]) for p in pages],
            figures,
            {
                "source_type": "pdf",
                "path": str(path.resolve()),
                "work_dir": str(work_dir.resolve()),
            },
            limitations,
        )
