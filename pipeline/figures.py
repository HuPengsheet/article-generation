"""Best-effort extraction of figures from pinned arXiv LaTeX sources.

No TeX is executed. Official HTML supplements unavailable source figures;
figure failures never invalidate the PDF evidence path.
"""

import gzip
import io
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

import pymupdf

from pipeline.arxiv import request_bytes

MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_SOURCE_FILES = 5000
MAX_IMAGE_PIXELS = 40_000_000


def _within(root, relative):
    if "\\" in relative or "\x00" in relative:
        raise ValueError("源码包包含无效路径。")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("源码包包含越界路径。")
    return target


def unpack_source(raw, destination):
    """Accept tar(.gz), gzip single TeX, and plain TeX; reject unsafe archives."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("源码包超过大小限制。")
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("解压后的源码超过大小限制。")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.ReadError:
        text = raw.decode("utf-8", errors="replace")
        if not re.search(r"\\(?:documentclass|begin\{document\})", text):
            raise ValueError("返回内容不是可识别的 LaTeX 源码。")
        (destination / "main.tex").write_text(text, encoding="utf-8")
        return destination
    with archive:
        total = 0
        members = []
        for member in archive:
            if len(members) >= MAX_SOURCE_FILES:
                raise ValueError("源码包文件数量超过限制。")
            target = _within(destination, member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("源码包包含链接或特殊文件。")
            total += member.size
            if total > MAX_SOURCE_BYTES:
                raise ValueError("源码包展开大小超过限制。")
            members.append((member, target))
        # Validate every member before writing anything.
        for member, target in members:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    return destination


def fetch_source(paper, directory, limitations=None):
    limitations = limitations if limitations is not None else []
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source_dir = directory / "source"
    if source_dir.is_dir() and any(source_dir.rglob("*.tex")):
        return source_dir
    stage = "源码获取（网络或缓存读取）"
    try:
        cache = directory / "source.bin"
        raw = (
            cache.read_bytes()
            if cache.exists()
            else request_bytes("https://arxiv.org/e-print/" + paper["version_id"])
        )
        stage = "源码解包拒绝或内容无效"
        with tempfile.TemporaryDirectory(prefix="source-", dir=directory) as temporary:
            unpack_source(raw, Path(temporary))
            if not any(Path(temporary).rglob("*.tex")):
                raise ValueError("源码包没有可读取的 .tex 文件。")
            if source_dir.exists():
                shutil.rmtree(source_dir)
            shutil.move(temporary, source_dir)
        cache.write_bytes(raw)
        return source_dir
    except Exception as error:
        limitations.append(f"论文配图不可用：{stage}失败（{type(error).__name__}）。")
        return None


def strip_comments(text):
    lines = []
    for line in text.splitlines():
        for index, char in enumerate(line):
            if char == "%":
                preceding = len(line[:index]) - len(line[:index].rstrip("\\"))
                if preceding % 2 == 0:
                    line = line[:index]
                    break
        lines.append(line)
    return "\n".join(lines)


def _group(text, offset, opener="{", closer="}"):
    """Read a balanced group, including nested braces and escaped delimiters."""
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if offset >= len(text) or text[offset] != opener:
        return None, offset
    start = offset + 1
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    return None, offset


def _arguments(text, command):
    for match in re.finditer(r"\\" + command + r"\*?(?![A-Za-z])", text):
        offset = match.end()
        _, after = _group(text, offset, "[", "]")
        value, _ = _group(text, after)
        if value is not None:
            yield match.start(), value


def _plain_caption(value):
    # Balanced arguments matter: citation keys and formatting may contain braces.
    commands = r"\\(?:cite\w*|[cC]ref|[eE]qref|[aA]utoref|ref|label|vspace|hspace)\*?(?![A-Za-z])"
    for match in reversed(list(re.finditer(commands, value))):
        offset = match.end()
        for _ in range(2):
            optional, after = _group(value, offset, "[", "]")
            if optional is None:
                break
            offset = after
        argument, after = _group(value, offset)
        if argument is not None:
            value = value[: match.start()] + value[after:]
    wrappers = r"\\(?:textbf|textit|emph|texttt|textsc|textrm|mathrm|mathbf|mathit|operatorname)(?![A-Za-z])"
    for match in reversed(list(re.finditer(wrappers, value))):
        argument, after = _group(value, match.end())
        if argument is not None:
            value = value[: match.start()] + argument + value[after:]
    value = re.sub(r"(?<!\\)\$", "", value)
    value = value.replace(r"\(", "").replace(r"\)", "").replace(r"\[", "").replace(r"\]", "")
    value = value.replace(r"\xspace", "").replace(r"\%", "%").replace(r"\&", "&").replace("~", " ")
    return " ".join(value.split())


def _subcaptions(body, clean=_plain_caption):
    """Associate images with environment and subfloat captions, in source order."""
    spans = []
    for block in re.finditer(r"\\begin\{subfigure\*?\}(.*?)\\end\{subfigure\*?\}", body, re.S):
        captions = list(_arguments(block[1], "caption"))
        spans.append((block.start(), block.end(), clean(captions[-1][1]) if captions else ""))
    for match in re.finditer(r"\\(?:subfloat|subfigure)(?![A-Za-z])", body):
        caption, offset = _group(body, match.end(), "[", "]")
        # subfloat can have a second optional (list-entry) caption argument.
        second, after = _group(body, offset, "[", "]")
        if second is not None:
            caption, offset = second, after
        content, after = _group(body, offset)
        if content is not None:
            spans.append((match.start(), after, clean(caption or "")))
    return sorted(spans)


def _convert_image(image, output):
    """Validate dimensions and convert supported raster/PDF inputs to PNG."""
    with pymupdf.open(image) as doc:
        if not doc.page_count:
            raise ValueError("empty image")
        page = doc[0]
        scale = 200 / 72 if doc.is_pdf else 1
        if page.rect.width * page.rect.height * scale**2 > MAX_IMAGE_PIXELS:
            raise ValueError("image too large")
        with Path(image).open("rb") as stream:
            header = stream.read(24)
        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        if (
            is_png
            and int.from_bytes(header[16:20], "big") * int.from_bytes(header[20:24], "big")
            > MAX_IMAGE_PIXELS
        ):
            raise ValueError("image too large")
        if is_png:
            if Path(image).resolve() != Path(output).resolve():
                shutil.copyfile(image, output)
        else:
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(output)


def _merge_panels(panels, output):
    """Keep legends with their plots instead of exposing a legend as a figure."""
    legends = [p for p in panels if "legend" in Path(p["source_file"]).stem.lower()]
    plots = [p for p in panels if p not in legends]
    columns = 3 if len(plots) == 3 or len(plots) >= 5 else 2 if len(plots) >= 2 else 1
    caption = panels[0].get("caption_en", "").lower()
    plot_sizes = [pymupdf.Pixmap(str(p["_path"])) for p in plots]
    if any(size.width / size.height > 3 for size in plot_sizes) or (
        "top" in caption and "bottom" in caption
    ):
        columns = 1
    width, gap = 1800, 20
    cell_width = (width - gap * (columns + 1)) / columns
    rectangles = []
    y = gap
    for legend in legends:
        legend["position"] = "legend above plots"
        pixmap = pymupdf.Pixmap(str(legend["_path"]))
        height = (width - 2 * gap) * pixmap.height / pixmap.width
        rectangles.append((legend["_path"], pymupdf.Rect(gap, y, width - gap, y + height)))
        y += height + gap
    for start in range(0, len(plots), columns):
        row = plots[start : start + columns]
        sizes = [pymupdf.Pixmap(str(p["_path"])) for p in row]
        row_height = max(cell_width * pixmap.height / pixmap.width for pixmap in sizes)
        for col, panel in enumerate(row):
            panel["position"] = f"row {start // columns + 1}, column {col + 1}"
            x = gap + col * (cell_width + gap)
            rectangles.append((panel["_path"], pymupdf.Rect(x, y, x + cell_width, y + row_height)))
        y += row_height + gap
    if width * y > MAX_IMAGE_PIXELS:
        raise ValueError("合并后的图片超过大小限制。")
    with pymupdf.open() as doc:
        page = doc.new_page(width=width, height=y)
        for path, rectangle in rectangles:
            page.insert_image(rectangle, filename=str(path), keep_proportion=True)
        page.get_pixmap(alpha=False).save(output)


def extract_figures(source_dir, out_dir, limitations=None):
    limitations = limitations if limitations is not None else []
    root = Path(source_dir).resolve()
    out_dir = Path(out_dir)
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(root.rglob("*.tex"))
    contents = {p: strip_comments(p.read_text(encoding="utf-8", errors="replace")) for p in sources}
    mains = [p for p in sources if re.search(r"\\documentclass\b", contents[p])]
    if not mains:
        limitations.append("论文配图不可用：没有找到含 documentclass 的主文件。")
        return []
    main = min(mains, key=lambda p: (p.name not in ("main.tex", "paper.tex"), len(p.parts), str(p)))
    if len(mains) > 1:
        limitations.append(f"源码包含多个主文件，配图提取使用 {main.relative_to(root)}。")

    def inline(path, stack=()):
        if path in stack or len(stack) > 50:
            limitations.append("跳过循环或过深的 LaTeX input/include。")
            return ""
        text = contents[path]
        pattern = r"\\(?:input|include)(?![A-Za-z])\s*(?:\{([^{}]+)\}|([^\s{}]+))"

        def expand(match):
            name = (match[1] or match[2]).strip()
            if not Path(name).suffix:
                name += ".tex"
            candidates = [path.parent / name, root / name]
            target = next(
                (
                    p.resolve()
                    for p in candidates
                    if p.resolve().is_relative_to(root) and p.resolve() in contents
                ),
                None,
            )
            if target is None:
                limitations.append(f"未展开 LaTeX 输入文件：{name}。")
                return ""
            return inline(target, (*stack, path))

        return re.sub(pattern, expand, text)

    text = inline(main)
    if "\\begin{document}" in text:
        # Figure-shaped examples/macros in the preamble are not paper figures.
        _, document = text.split("\\begin{document}", 1)
        graphics_text = text
        text = document.split("\\end{document}", 1)[0]
    else:
        graphics_text = text
    graphics_dirs = [root, main.parent]
    for _, group in _arguments(graphics_text, "graphicspath"):
        for folder in re.findall(r"\{([^{}]+)\}", group):
            target = (main.parent / folder).resolve()
            if target.is_relative_to(root):
                graphics_dirs.append(target)
    # Search include directories too, for sources using section-relative images.
    graphics_dirs.extend(p.parent for p in sources)
    graphics_dirs = list(dict.fromkeys(graphics_dirs))

    def resolve_image(name):
        if "\\" in name or "#" in name:
            return None
        names = (
            [name]
            if Path(name).suffix
            else [name + ext for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps")]
        )
        for directory in graphics_dirs:
            for candidate_name in names:
                candidate = (directory / candidate_name).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    return candidate
        return None

    # Expand only literal, zero-argument caption macros. No TeX is executed.
    caption_macros = {}
    for match in re.finditer(
        r"\\(?:newcommand|renewcommand|providecommand)\*?(?![A-Za-z])", graphics_text
    ):
        name, offset = _group(graphics_text, match.end())
        arity, after = _group(graphics_text, offset, "[", "]")
        value, _ = _group(graphics_text, after)
        if (
            name
            and re.fullmatch(r"\\[A-Za-z]+", name)
            and arity in (None, "0")
            and value is not None
            and "#" not in value
        ):
            value = value.replace(r"\xspace", "").strip()
            while value.startswith("{"):
                inner, end = _group(value, 0)
                if inner is None or end != len(value):
                    break
                value = inner.strip()
            caption_macros[name] = value

    def clean_caption(value):
        for _ in range(5):
            expanded = re.sub(r"\\[A-Za-z]+", lambda m: caption_macros.get(m[0], m[0]), value)
            if expanded == value:
                break
            value = expanded
        return _plain_caption(value)

    results = []
    pattern = r"\\begin\{(figure\*?|wrapfigure)\}(.*?)\\end\{\1\}"
    order = 0
    for block in re.finditer(pattern, text, re.S):
        body = block[2]
        subcaptions = _subcaptions(body, clean_caption)
        caption_body = body
        for start, end, _ in reversed(subcaptions):
            caption_body = caption_body[:start] + caption_body[end:]
        captions = list(_arguments(caption_body, "caption"))
        caption = clean_caption(captions[-1][1]) if captions else ""
        labels = list(_arguments(caption_body, "label"))
        label = labels[-1][1] if labels else ""
        graphics = list(_arguments(body, "includegraphics"))
        if not captions and not graphics:
            # Tables are sometimes placed in wrapfigure environments. They must
            # not shift the source figure numbers (TRACE has three such blocks).
            limitations.append("跳过无图注、无图片的 figure 环境（可能为表格宏）。")
            continue
        order += 1
        if not graphics:
            limitations.append(
                f"原图 {order} 没有可直接提取的 includegraphics（可能为宏或 TikZ 图）。"
            )
            continue
        if not caption:
            limitations.append(f"原图 {order} 缺少可提取的外层图注，未提供给写作模型。")
            continue
        panels = []
        for panel, (offset, name) in enumerate(graphics, 1):
            image = resolve_image(name.strip())
            if image is None:
                limitations.append(f"原图 {order} 图片路径无法解析：{name}。")
                continue
            if image.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg"):
                limitations.append(f"原图 {order} 图片格式暂不支持：{image.suffix}。")
                continue
            filename = f"figure-{order:02d}-{panel:02d}.png"
            output = figures_dir / filename
            try:
                _convert_image(image, output)
                subcaption = next(
                    (
                        (i, c)
                        for i, (start, end, c) in enumerate(subcaptions)
                        if start <= offset < end
                    ),
                    None,
                )
                panels.append(
                    {
                        "file": f"figures/{filename}",
                        "caption_en": caption,
                        "order": order,
                        "panel": panel,
                        "label": label,
                        "source_file": str(image.relative_to(root)),
                        "_path": output,
                        "subcaption_en": subcaption[1] if subcaption else "",
                        "subfigure_label": f"({chr(97 + subcaption[0])})" if subcaption else "",
                        "position": "single",
                    }
                )
            except Exception as error:
                output.unlink(missing_ok=True)
                limitations.append(
                    f"原图 {order} 第 {panel} 张图片转换失败（{type(error).__name__}）。"
                )
        if len(panels) > 1:
            merged = figures_dir / f"figure-{order:02d}.png"
            try:
                _merge_panels(panels, merged)
                results.append(
                    {
                        "file": f"figures/{merged.name}",
                        "caption_en": caption,
                        "order": order,
                        "label": label,
                        "source_files": [p["source_file"] for p in panels],
                        "panel_count": len(panels),
                        "expected_panel_count": len(graphics),
                        "complete": len(panels) == len(graphics),
                        "origin": "latex",
                        "panels": [_panel_metadata(p) for p in panels],
                    }
                )
            except Exception as error:
                limitations.append(
                    f"原图 {order} 子图合并失败（{type(error).__name__}），保留单独子图。"
                )
                results.extend(
                    {
                        **{k: v for k, v in p.items() if k != "_path"},
                        "origin": "latex",
                        "expected_panel_count": len(graphics),
                        "complete": len(panels) == len(graphics),
                        "panels": [_panel_metadata(p)],
                    }
                    for p in panels
                )
        else:
            results.extend(
                {
                    **{k: v for k, v in p.items() if k != "_path"},
                    "origin": "latex",
                    "expected_panel_count": len(graphics),
                    "complete": len(panels) == len(graphics),
                    "panels": [_panel_metadata(p)],
                }
                for p in panels
            )
    if not results:
        limitations.append("未提取到可用论文配图，文章按纯文字生成。")
    return results


def _panel_metadata(panel):
    return {
        "position": panel.get("position", "single"),
        "caption_en": panel.get("subcaption_en", ""),
        "label": panel.get("subfigure_label", ""),
        "source_file": panel["source_file"],
        "image_index": panel.get("panel", 1),
    }


def collect_figures(paper, directory):
    limitations = []
    source = fetch_source(paper, directory, limitations)
    figures = []
    if source is not None:
        try:
            figures = extract_figures(source, directory, limitations)
        except Exception as error:
            limitations.append(f"论文源码配图提取失败（{type(error).__name__}）。")
    # Only request HTML when source extraction is incomplete. Do not replace
    # successfully extracted figures or renumber a partially available paper.
    needs_html = not figures or any(
        any(
            term in item
            for term in (
                "没有可直接提取",
                "无法解析",
                "暂不支持",
                "转换失败",
                "缺少可提取",
                "提取失败",
            )
        )
        for item in limitations
    )
    if needs_html:
        from pipeline.html_figures import collect_html_figures

        try:
            known = {figure["order"] for figure in figures}
            # An incomplete source figure is replaced only by a complete HTML one.
            incomplete = {
                f["order"]
                for f in figures
                if any(
                    f"原图 {f['order']} " in item
                    and any(
                        term in item for term in ("无法解析", "暂不支持", "转换失败", "缺少可提取")
                    )
                    for item in limitations
                )
            }
            fallback = collect_html_figures(
                paper, directory, limitations, skip_orders=known - incomplete
            )
            replacements = {f["order"]: f for f in fallback if f["order"] in incomplete}
            figures = [f for f in figures if f["order"] not in replacements]
            figures.extend(
                f for f in fallback if f["order"] not in known or f["order"] in replacements
            )
            figures.sort(key=lambda f: f["order"])
        except Exception as error:
            limitations.append(f"官方 HTML 配图兜底失败（{type(error).__name__}）。")
    # Earlier source failures stay visible, but should not claim recovered output
    # is text-only.
    limitations = [item for item in limitations if not (figures and "按纯文字生成" in item)]
    if not figures:
        limitations.append("源码与官方 HTML 均未取得可用图片，文章按纯文字生成。")
    return figures, limitations
