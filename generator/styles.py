"""Load, validate, and inject writing style configurations."""

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLES_DIR = ROOT / "config" / "styles"


@dataclass
class Style:
    name: str
    metadata: dict
    guide_text: str
    examples_text: str

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
    return Style(config["name"], config, guide, examples or "")


def available_styles(styles_dir=None):
    directory = Path(styles_dir or STYLES_DIR)
    if not directory.is_dir():
        return []
    return sorted(
        d.name
        for d in directory.iterdir()
        if d.is_dir() and (d / "style.json").is_file()
    )


def inject_style(prompt, style):
    """Append the style guide to a system prompt."""
    if style is None:
        return prompt
    return prompt + "\n\n## 风格要求\n\n" + style.guide_text


def style_context(style):
    """Format examples as a user-context fragment for evidence JSON."""
    if style is None or not style.examples_text:
        return ""
    return style.examples_text


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
