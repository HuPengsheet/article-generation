"""Explicit, caller-reviewed editorial material; never inferred from an angle."""

from pathlib import Path
from urllib.parse import urlsplit

from generator.figures.latex import _convert_image


def validate_team_context(context):
    if context is None:
        return None
    if not isinstance(context, dict) or context.get("verified") is not True:
        raise ValueError("团队背景需要调用者核实并标记 verified=true。")
    description, sources = context.get("description"), context.get("sources")
    if not isinstance(description, str) or not description.strip() or len(description) > 3000:
        raise ValueError("团队背景需要 1–3000 字符的事实说明。")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise ValueError("团队背景需要 1–8 个已核实来源。")
    normalized = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("团队背景来源格式无效。")
        title, url = source.get("title"), source.get("url")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 200
            or not isinstance(url, str)
            or len(url) > 2000
        ):
            raise ValueError("团队背景来源缺少标题或网址。")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("团队背景来源必须为有效 HTTPS 网址。")
        normalized.append({"title": title, "url": url})
    # Format validation is not an independent verification of fame or affiliation.
    return {
        "verified": True,
        "description": description,
        "sources": normalized,
        "verification_method": "caller_reviewed_sources",
    }


def prepare_illustrations(illustrations, directory):
    """Register caller-created PNG diagrams for prompts, preview and ZIP export."""
    if illustrations is None:
        return []
    if not isinstance(illustrations, list) or len(illustrations) > 8:
        raise ValueError("自绘素材需要列表，最多 8 张。")
    directory = Path(directory)
    destination = directory / "figures"
    destination.mkdir(exist_ok=True)
    figures = []
    for index, item in enumerate(illustrations, 1):
        if not isinstance(item, dict):
            raise ValueError("自绘素材格式无效。")
        caption, assumptions = item.get("caption"), item.get("assumptions")
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 2000
            for value in (caption, assumptions)
        ):
            raise ValueError("自绘素材必须有图注和假设说明，且各不超过 2000 字符。")
        path = item.get("path")
        if not isinstance(path, (str, Path)):
            raise ValueError("自绘素材需要本地 PNG 路径。")
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("自绘素材不存在或超过大小限制。")
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("自绘素材必须是 PNG 图片。")
        output = destination / f"illustration-{index:02d}.png"
        _convert_image(path, output)
        figures.append(
            {
                "file": "figures/" + output.name,
                "order": 0,
                "origin": "author_diagram",
                "caption_en": caption,
                "assumptions": assumptions,
                "source_file": path.name,
                "panels": [],
            }
        )
    return figures
