"""Official arXiv HTML fallback, preserving figure and subfigure captions."""

import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from pipeline.arxiv import request_bytes

MAX_HTML_BYTES = 15 * 1024 * 1024
MAX_ASSET_BYTES = 30 * 1024 * 1024
MAX_HTML_FIGURES = 200


class _Node:
    def __init__(self, tag="", attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self):
        if self.tag == "math" and self.attrs.get("alttext"):
            return self.attrs["alttext"]
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children)


class _Document(HTMLParser):
    VOID = {
        "img",
        "br",
        "hr",
        "meta",
        "link",
        "input",
        "source",
        "wbr",
        "area",
        "base",
        "embed",
        "param",
        "col",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = _Node()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(_Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _figure_parent(node):
    node = node.parent
    while node is not None:
        if node.tag == "figure":
            return node
        node = node.parent
    return None


def _caption(figure):
    return next(
        (
            " ".join(n.text().split())
            for n in figure.walk()
            if n.tag == "figcaption" and _figure_parent(n) is figure
        ),
        "",
    )


def parse_html_figures(markup):
    document = _Document()
    document.feed(markup)
    results = []
    for figure in document.root.walk():
        if figure.tag != "figure" or _figure_parent(figure) is not None:
            continue
        classes = figure.attrs.get("class", "").split()
        if "ltx_figure" not in classes:
            continue
        caption = _caption(figure)
        # Never renumber by availability: Figure 2 must stay 2 when Figure 1
        # is unsupported or missing. Unnumbered HTML figures cannot be matched.
        number = re.search(r"(?:Figure|Fig\.)\s*(\d+)\s*[:.]?", caption, re.I)
        if not number:
            continue
        panels = []
        for image in figure.walk():
            if image.tag != "img" or not image.attrs.get("src"):
                continue
            owner = _figure_parent(image)
            subcaption = _caption(owner) if owner is not figure else ""
            label = re.match(r"\s*(\([a-z]\))", subcaption)
            panels.append(
                {
                    "src": image.attrs["src"],
                    "caption_en": subcaption,
                    "label": label[1] if label else "",
                }
            )
        results.append(
            {
                "order": int(number[1]),
                "caption_en": caption,
                "label": figure.attrs.get("id", ""),
                "panels": panels,
            }
        )
        if len(results) >= MAX_HTML_FIGURES:
            break
    return results


def _asset_url(source, page_url, version_id):
    url = urljoin(page_url, source)
    parts = urlsplit(url)
    path = unquote(parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "arxiv.org"
        or not path.startswith("/html/" + version_id + "/")
        or ".." in PurePosixPath(path).parts
        or "\\" in path
        or "\x00" in path
    ):
        raise ValueError("HTML 图片不属于固定版本的 arXiv 路径。")
    return url


def collect_html_figures(paper, directory, limitations, skip_orders=()):
    from pipeline.figures import _convert_image, _merge_panels, _panel_metadata

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    version = paper["version_id"]
    page_url = "https://arxiv.org/html/" + version
    cache = directory / "paper.html"
    raw = cache.read_bytes() if cache.exists() else request_bytes(page_url)
    if len(raw) > MAX_HTML_BYTES:
        raise ValueError("HTML 大小超过限制。")
    candidates = parse_html_figures(raw.decode("utf-8", errors="replace"))
    if not candidates:
        limitations.append("官方 HTML 未提供带编号、图注及可下载素材的配图。")
        return []
    cache.write_bytes(raw)
    figures_dir = directory / "figures"
    figures_dir.mkdir(exist_ok=True)
    results = []
    for figure in candidates:
        if figure["order"] in skip_orders:
            continue
        panels = []
        failed = False
        for index, panel in enumerate(figure["panels"], 1):
            output = figures_dir / f"html-figure-{figure['order']:02d}-{index:02d}.png"
            try:
                url = _asset_url(panel["src"], page_url, version)
                if output.exists():
                    _convert_image(output, output)
                else:
                    image = request_bytes(url)
                    if len(image) > MAX_ASSET_BYTES:
                        raise ValueError("HTML 图片大小超过限制。")
                    suffix = Path(urlsplit(url).path).suffix.lower()
                    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
                        raise ValueError("HTML 图片格式暂不支持。")
                    with tempfile.TemporaryDirectory(prefix="html-image-", dir=directory) as temp:
                        source = Path(temp) / ("image" + suffix)
                        source.write_bytes(image)
                        _convert_image(source, output)
                panels.append(
                    {
                        "file": "figures/" + output.name,
                        "_path": output,
                        "source_file": url,
                        "caption_en": figure["caption_en"],
                        "subcaption_en": panel["caption_en"],
                        "subfigure_label": panel["label"],
                        "panel": index,
                        "position": "single",
                    }
                )
            except Exception as error:
                output.unlink(missing_ok=True)
                failed = True
                limitations.append(
                    f"HTML 原图 {figure['order']} 子图 {index} 获取或转换失败（{type(error).__name__}）。"
                )
        # A fallback must not silently present an incomplete composite as complete.
        if failed or not panels:
            continue
        if len(panels) > 1:
            output = figures_dir / f"html-figure-{figure['order']:02d}.png"
            try:
                _merge_panels(panels, output)
            except Exception as error:
                limitations.append(
                    f"HTML 原图 {figure['order']} 合并失败（{type(error).__name__}）。"
                )
                continue
            file = "figures/" + output.name
        else:
            file = panels[0]["file"]
        results.append(
            {
                "file": file,
                "order": figure["order"],
                "label": figure["label"],
                "caption_en": figure["caption_en"],
                "origin": "arxiv_html",
                "source_url": page_url,
                "panel_count": len(panels),
                "complete": True,
                "panels": [_panel_metadata(p) for p in panels],
                "source_files": [p["source_file"] for p in panels],
            }
        )
    return results
