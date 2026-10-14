#!/usr/bin/env python3
"""Convert saved WeChat article pages into Markdown style templates plus layout stats.

Usage: python scripts/extract_template.py [template_dir] [--style NAME]

Each `template/<n>/*.html` becomes `config/styles/<style>/templates/<n>.md`; aggregate
numbers (length, sections, figures, caption length) go to `templates/stats.json` so the
writer can be held to the same layout density as the reference articles.
"""

import argparse
import html
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator.figures.html_img import _Document  # noqa: E402

SKIP = {"script", "style", "template", "svg", "mp-common-mpaudio", "button", "noscript"}
# First marker opens a promotion list; the rest mark the account footer.
TAIL_MARKERS = ("相关推荐", "交流加群请在", "预览时标签不可点")
# Account boilerplate at the top of every post.
HEAD_NOISE = re.compile(r"^(?:投稿/寻求报道|关键词[：:])")
SECTION = re.compile(r"^[一二三四五六七八九十]+、")


def _hidden(node):
    style = node.attrs.get("style", "").replace(" ", "")
    return "display:none" in style or "hidden" in node.attrs


def _inline(node):
    """Render inline content, keeping bold/italic marks the writer should imitate."""
    if isinstance(node, str):
        return node
    if node.tag in SKIP or _hidden(node):
        return ""
    if node.tag == "br":
        return "\n"
    if node.tag == "img":
        return ""
    text = "".join(_inline(child) for child in node.children)
    if node.tag in ("strong", "b") and text.strip():
        # Nested emphasis from the editor would produce unbalanced markers, and
        # CommonMark ignores `**` beside CJK punctuation, so keep punctuation outside.
        match = re.match(r"^([\s\W_]*)(.*?)([\s\W_]*)$", re.sub(r"\*+", "", text), re.S)
        lead, core, tail = match.groups()
        return f"{lead.strip()}**{core}**{tail.strip()}" if core else text
    if node.tag == "em" and text.strip() and "*" not in text:
        return f"*{text.strip()}*"
    return text


def _clean(text):
    text = html.unescape(text).replace("\xa0", " ")
    # Adjacent bold spans from the WeChat editor collapse into one mark.
    text = re.sub(r"\*\*(\s*)\*\*", r"\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _blocks(node, out):
    """Flatten the article DOM into (kind, text) blocks in reading order."""
    if isinstance(node, str) or node.tag in SKIP or _hidden(node):
        return
    if node.tag in ("h1", "h2", "h3", "h4"):
        text = _clean(re.sub(r"\*+", "", _inline(node)))
        if text:
            out.append(("h" + node.tag[1], text))
        return
    if node.tag == "figure":
        caption = next((c for c in node.walk() if c.tag == "figcaption"), None)
        if any(c.tag == "img" for c in node.walk()):
            out.append(("img", _clean(_inline(caption)) if caption else ""))
        return
    if node.tag == "img":
        out.append(("img", ""))
        return
    if node.tag in ("p", "li", "blockquote"):
        # Images may sit inside paragraphs in the WeChat editor.
        if any(c.tag == "img" for c in node.walk()):
            out.append(("img", ""))
        text = _clean(_inline(node))
        if text:
            out.append(({"p": "p", "li": "li", "blockquote": "quote"}[node.tag], text))
        return
    for child in node.children:
        _blocks(child, out)


def _title(markup):
    match = re.search(r'<meta property="og:title" content="([^"]*)"', markup)
    return html.unescape(match.group(1)).strip() if match else ""


def extract(markup):
    """Return (title, blocks) for one saved WeChat page."""
    document = _Document()
    document.feed(markup)
    body = next((n for n in document.root.walk() if n.attrs.get("id") == "js_content"), None)
    if body is None:
        raise ValueError("页面中没有找到公众号正文 js_content。")
    raw = []
    _blocks(body, raw)
    # Promotion lists ("相关推荐" + links) also appear mid-article; drop each with its
    # links, and stop at the account footer.
    footer = [
        i
        for i, (k, t) in enumerate(raw)
        if k != "img" and t.strip("* ").startswith(TAIL_MARKERS[1:])
    ]
    # The group invitation is repeated after the table of contents; only the last one
    # is the footer.
    end = footer[-1] if footer else len(raw)
    start = end
    while start > 0 and raw[start - 1][0] == "li":
        start -= 1
    if start > 0 and raw[start - 1][1].strip("* ") == TAIL_MARKERS[0]:
        end = start - 1
    blocks, skipping = [], False
    for index, (kind, text) in enumerate(raw[:end]):
        plain = text.strip("* ")
        if index in footer:
            continue
        if kind == "p" and plain in (TAIL_MARKERS[0], "本文目录"):
            skipping = True
            continue
        if kind == "h2" and plain == "本文目录":
            skipping = True
            continue
        if skipping and kind == "li":
            continue
        skipping = False
        if kind == "p" and HEAD_NOISE.match(plain):
            continue
        blocks.append((kind, text))
    # The editor's table of contents repeats section headings as list items.
    headings = {text for kind, text in blocks if kind.startswith("h")}
    blocks = [
        (kind, text)
        for kind, text in blocks
        if not (kind == "li" and re.sub(r"\*+", "", text).strip() in headings)
    ]
    # Some captions are separate paragraphs right after the image.
    merged = []
    for kind, text in blocks:
        if (
            kind == "p"
            and merged
            and merged[-1] == ("img", "")
            and re.match(r"^\*{0,2}(?:图|Figure|Fig\.)\s*\d", text)
        ):
            merged[-1] = ("img", text.strip("* "))
            continue
        merged.append((kind, text))
    return _title(markup), merged


def to_markdown(title, blocks):
    lines = [f"# {title}"] if title else []
    figure = 0
    for kind, text in blocks:
        if kind == "img":
            figure += 1
            lines.append(f"![图{figure}](figure-{figure:02d}.png)")
            if text:
                lines.append(f"*{text}*")
        elif kind in ("h1", "h2"):
            lines.append(f"## {text}")
        elif kind in ("h3", "h4"):
            lines.append(f"### {text}")
        elif kind == "li":
            lines.append(f"- {text}")
        else:
            # The editor styles section intros as blockquotes; they read as plain prose.
            lines.append(text)
    return "\n\n".join(lines) + "\n"


def stats(title, blocks):
    prose = [t for k, t in blocks if k in ("p", "li", "quote")]
    chars = sum(len(re.sub(r"[\s*]", "", t)) for t in prose)
    captions = [len(t) for k, t in blocks if k == "img" and t]
    figures = sum(1 for k, _ in blocks if k == "img")
    return {
        "title": title,
        "title_chars": len(title),
        "body_chars": chars,
        "sections": sum(1 for k, t in blocks if k in ("h1", "h2") and SECTION.match(t)),
        "subsections": sum(1 for k, _ in blocks if k in ("h3", "h4")),
        "paragraphs": len(prose),
        "figures": figures,
        "chars_per_figure": round(chars / figures) if figures else None,
        "caption_chars_median": round(statistics.median(captions)) if captions else 0,
        "bold_paragraphs": sum(1 for t in prose if t.startswith("**")),
    }


def summarize(items):
    def span(key):
        values = [item[key] for item in items if item[key] is not None]
        return {
            "min": min(values),
            "median": round(statistics.median(values)),
            "max": max(values),
        }

    keys = ("body_chars", "sections", "figures", "chars_per_figure", "caption_chars_median")
    return {key: span(key) for key in keys}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template_dir", nargs="?", default=str(ROOT / "template"))
    parser.add_argument("--style", default="ai-infra-deep")
    args = parser.parse_args(argv)
    source = Path(args.template_dir)
    target = ROOT / "config" / "styles" / args.style / "templates"
    if not (target.parent / "style.json").is_file():
        print(f"ERROR: 风格 {args.style} 不存在。")
        return 1
    pages = sorted(p for p in source.glob("*/*.html"))
    if not pages:
        print(f"ERROR: {source} 下没有 */*.html 模板。")
        return 1
    target.mkdir(exist_ok=True)
    items = []
    for page in pages:
        title, blocks = extract(page.read_text(encoding="utf-8", errors="replace"))
        name = page.parent.name
        (target / f"{name}.md").write_text(to_markdown(title, blocks), encoding="utf-8")
        items.append({"file": f"{name}.md", **stats(title, blocks)})
        print(f"{name}.md  {items[-1]['body_chars']} 字  {items[-1]['figures']} 图  {title}")
    result = {"articles": items, "summary": summarize(items)}
    (target / "stats.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"DONE: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
