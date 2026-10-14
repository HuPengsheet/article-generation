"""Load, validate, and inject writing style configurations."""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLES_DIR = ROOT / "config" / "styles"


@dataclass
class Style:
    name: str
    metadata: dict
    guide_text: str
    examples_text: str
    # Full reference articles from scripts/extract_template.py: [(name, markdown)].
    templates: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def banned_phrases(self):
        return self.metadata.get("constraints", {}).get("banned_phrases", [])


def load_style(name=None, *, styles_dir=None):
    """Load a named style or the default. Returns None when name is explicitly None
    and no default exists, so callers can proceed without style constraints."""
    directory = Path(styles_dir or STYLES_DIR)
    if name is not None:
        target = directory / name
        if not target.is_dir():
            raise ValueError(f"风格 {name!r} 不存在；可用：{available_styles(directory)}")
    else:
        target = _find_default(directory)
        if target is None:
            return None
    config = json.loads((target / "style.json").read_text(encoding="utf-8"))
    guide = _read_optional(target / "guide.md")
    examples = _read_optional(target / "examples.md")
    if not guide:
        raise ValueError(f"风格 {config['name']} 缺少 guide.md。")
    templates_dir = target / "templates"
    templates = [
        (path.stem, path.read_text(encoding="utf-8")) for path in sorted(templates_dir.glob("*.md"))
    ]
    stats_path = templates_dir / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
    return Style(config["name"], config, guide, examples or "", templates, stats)


def available_styles(styles_dir=None):
    directory = Path(styles_dir or STYLES_DIR)
    if not directory.is_dir():
        return []
    return sorted(
        d.name for d in directory.iterdir() if d.is_dir() and (d / "style.json").is_file()
    )


def inject_style(prompt, style):
    """Append the style guide to a system prompt."""
    if style is None:
        return prompt
    return prompt + "\n\n## 风格要求\n\n" + style.guide_text


def style_context(style):
    """Short examples, used only when a style has no full reference articles."""
    if style is None or style.templates or not style.examples_text:
        return ""
    return style.examples_text


def pick_templates(style, key, count=2):
    """Pick full reference articles; the key keeps one article's retries consistent
    while different articles see different references."""
    if style is None or not style.templates:
        return []
    start = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % len(style.templates)
    chosen = [style.templates[(start + i) % len(style.templates)] for i in range(count)]
    return [{"name": name, "markdown": text} for name, text in dict(chosen).items()]


def template_layouts(style):
    """Per-section length and figure counts of every reference article, for outlining."""
    layouts = []
    for name, text in style.templates if style else []:
        title = next(iter(re.findall(r"^# (.+)$", text, re.M)), "")
        sections = []
        for part in re.split(r"^## ", text, flags=re.M)[1:]:
            heading, _, body = part.partition("\n")
            sections.append(
                {
                    "heading": heading.strip(),
                    "subsections": re.findall(r"^### (.+)$", body, re.M),
                    "chars": prose_chars(body),
                    "figures": len(re.findall(r"^!\[", body, re.M)),
                }
            )
        layouts.append({"name": name, "title": title, "sections": sections})
    return layouts


def prose_chars(markdown):
    """Count body characters, ignoring headings, images, citations, marks and spaces."""
    lines = [line for line in markdown.splitlines() if not re.match(r"\s*(?:#|!\[)", line)]
    text = re.sub(r"\[S\d+:B\d+\]", "", "\n".join(lines))
    return len(re.sub(r"[\s*_>`|-]", "", text))


# Lower bounds per depth; reference articles set the target above them.
DEPTH_MIN_CHARS = {"brief": 1500, "detailed": 4000, "deep-dive": 6000}


def writing_targets(style, depth, figure_count, source_chars):
    """Length and figure floors the outline and article validators enforce."""
    summary = (style.stats.get("summary") if style else None) or {}
    median_figures = summary.get("figures", {}).get("median", 6)
    # Short sources cannot support a long article without padding.
    min_chars = min(DEPTH_MIN_CHARS[depth], source_chars // 4)
    min_figures = min(figure_count, max(4, math.ceil(figure_count * 0.6)), median_figures)
    targets = {"min_chars": min_chars, "min_figures": min_figures}
    if summary:
        targets["reference"] = {
            key: summary[key]
            for key in ("body_chars", "sections", "figures", "chars_per_figure")
            if key in summary
        }
    return targets


def _find_default(directory):
    if not directory.is_dir():
        return None
    for child in directory.iterdir():
        if not child.is_dir() or not (child / "style.json").is_file():
            continue
        config = json.loads((child / "style.json").read_text(encoding="utf-8"))
        if config.get("default"):
            return child
    return None


def _read_optional(path):
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""
