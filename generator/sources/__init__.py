"""Source registry: registering a format does not change the generation pipeline."""

from pathlib import Path
from urllib.parse import urlsplit

from generator.sources.base import ExtractedText, Figure, Material, Source
from generator.sources.html import HtmlSource
from generator.sources.markdown import MarkdownSource
from generator.sources.pdf import PdfSource
from generator.sources.url import UrlSource

_REGISTRY = []


def register(predicate, factory):
    _REGISTRY.insert(0, (predicate, factory))


def detect(path_or_url):
    value = str(path_or_url)
    for predicate, factory in _REGISTRY:
        if predicate(value):
            return factory()
    if urlsplit(value).scheme in ("http", "https"):
        return UrlSource()
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"材料文件不存在：{value}")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("材料文件超过 50 MiB 限制。")
    with path.open("rb") as stream:
        signature = stream.read(5)
    if signature == b"%PDF-":
        return PdfSource()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raise ValueError("文件后缀为 PDF，但内容不是有效 PDF。")
    factory = {
        ".html": HtmlSource,
        ".htm": HtmlSource,
        ".md": MarkdownSource,
        ".markdown": MarkdownSource,
    }.get(suffix)
    if factory is None:
        raise ValueError(f"不支持的材料格式：{suffix or '无后缀'}")
    return factory()


__all__ = ["detect", "register", "Source", "Material", "Figure", "ExtractedText"]
