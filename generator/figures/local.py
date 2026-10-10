"""Copy and normalize source assets to validated PNGs, without executing content."""

import hashlib
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from generator.figures.latex import _convert_image
from generator.sources.base import Figure
from shared.http import fetch


def acquire_image(reference, base, work_dir, order, caption="", origin="local"):
    if not reference:
        raise ValueError("图片地址为空。")
    parsed = urlsplit(reference)
    if parsed.scheme or str(base).startswith(("https://", "http://")):
        url = urljoin(str(base), reference)
        response = fetch(url, limit=30 * 1024 * 1024)
        raw = response["content"]
        suffix = Path(urlsplit(response["url"]).path).suffix
        source = response["url"]
    else:
        if parsed.netloc:
            raise ValueError("本地图片不支持网络路径。")
        path = (Path(base).parent / unquote(parsed.path)).resolve()
        if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("本地图片不存在或过大。")
        raw, suffix, source = path.read_bytes(), path.suffix, str(path)
    # Normalize supported bitmap/vector source formats through the existing converter.
    name = f"image-{order:03d}-{hashlib.sha256(raw).hexdigest()[:10]}.png"
    destination = Path(work_dir) / "figures" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        original = Path(temporary) / ("asset" + (suffix or ".img"))
        original.write_bytes(raw)
        _convert_image(original, destination)
    return Figure("figures/" + name, caption, order, origin, metadata={"source_file": source})
