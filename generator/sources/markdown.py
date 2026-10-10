"""Markdown preserves headings, tables, code, and image captions."""

from pathlib import Path

from markdown_it import MarkdownIt

from generator.figures.local import acquire_image
from generator.sources.base import ExtractedText, Material, Source


class MarkdownSource(Source):
    def extract(self, path_or_url, work_dir, *, base_url=None):
        path, work_dir = Path(path_or_url), Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise ValueError("Markdown 内容为空。")
        lines = text.splitlines()
        tokens = MarkdownIt("commonmark").enable("table").parse(text)
        sections, figures, limitations = [], [], []
        heading, title = "", path.stem
        captured = set()
        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                heading = tokens[index + 1].content
                if not sections:
                    title = heading
            if token.map and token.level == 0 and tuple(token.map) not in captured:
                start, end = token.map
                captured.add(tuple(token.map))
                sections.append(
                    ExtractedText(f"lines {start + 1}-{end}", "\n".join(lines[start:end]), heading)
                )
            for child in token.children or []:
                if child.type == "image":
                    try:
                        figures.append(
                            acquire_image(
                                child.attrGet("src"),
                                base_url or str(path.resolve()),
                                work_dir,
                                len(figures) + 1,
                                child.content,
                                "markdown",
                            )
                        )
                    except (ValueError, RuntimeError, OSError) as error:
                        limitations.append(
                            f"图片未提取（{type(error).__name__}）：{child.attrGet('src')}"
                        )
        if not sections:
            sections = [ExtractedText(f"lines 1-{len(lines)}", text)]
        (work_dir / "source.md").write_text(text, encoding="utf-8")
        return Material(
            title,
            [],
            sections,
            figures,
            {
                "source_type": "markdown",
                "path": str(path.resolve()),
                "url": base_url,
                "work_dir": str(work_dir.resolve()),
            },
            limitations,
        )
